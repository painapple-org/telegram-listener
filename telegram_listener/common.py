"""Minimal shared helpers the listener/protocol layer actually needs.

This is a deliberately small subset of what `spoor`'s own
`scripts/shared/telegram_common.py` provides: bot-API base resolution,
transcript logging, and the pending-confirmation record used by callback
queries. A deployment's own activity-feed/dashboard plumbing does not live
here - that's a deployment-specific integration, not something a generic
listener needs. Anything downstream that wants to mirror the conversation
registers a `set_transcript_observer` callback instead (below), never a
hard import of this module from the other direction.
"""

import json
import os
import re
import socket
import sys
import threading
import time


def resolve_data_dir():
    explicit = os.environ.get("TELEGRAM_LISTENER_DATA_DIR")
    if explicit:
        return explicit
    if os.path.isdir("/data"):
        return "/data"
    return os.path.join(os.getcwd(), "data")


def resolve_bot_api_base(bot_token, service_hostname=None):
    """Resolve the self-hosted local Bot API server's base URL.

    Prefers the Docker-compose service hostname (see
    installer/docker-compose.yml) when it resolves, falling back to
    localhost for a bare host process talking to a port-published
    container. Both the hostname and the port are overridable for a
    deployment that publishes the container somewhere else.
    """
    service_hostname = service_hostname or os.environ.get("TELEGRAM_BOT_API_HOST", "telegram-bot-api")
    port = int(os.environ.get("TELEGRAM_BOT_API_PORT", "8081"))
    try:
        socket.gethostbyname(service_hostname)
        host = service_hostname
    except socket.gaierror:
        host = "127.0.0.1"
    return f"http://{host}:{port}/bot{bot_token}"


_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


def redact(text):
    """Strip the bot token out of `text` before it reaches a log file.

    The Bot API carries the token in the URL path, so a `requests`
    connection error, an HTTP error and any traceback through this code all
    quote it verbatim. The token is a password - anyone holding it can read
    and send this bot's messages - and log files get tailed, pasted into
    chat and shipped off the host, so it must never be written to one.
    """
    text = str(text)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        text = text.replace(token, "<redacted>")
    return _TOKEN_IN_URL.sub("/bot<redacted>", text)


TRANSCRIPT_FILE = os.path.join(resolve_data_dir(), "telegram-listener", "transcript.jsonl")

_TRANSCRIPT_LOCK = threading.Lock()

_transcript_observer = None


def set_transcript_observer(observer):
    """Call `observer(direction, entry, byte_offset)` after every transcript
    entry this process writes.

    A deployment that mirrors the conversation somewhere else - an activity
    feed, a dashboard, an audit index - needs to see every entry, including
    the ones the listener itself writes (incoming messages, its own sends,
    reactions). Wrapping the writers cannot do that, since the listener
    calls `append_transcript` directly; this is the seam that can. The byte
    offset is where the entry landed in `TRANSCRIPT_FILE`, so an observer
    can deep-link straight at it instead of re-scanning the file.

    `observer=None` removes it again. Set from the process that owns the
    integration; the listener core never sets one itself.
    """
    global _transcript_observer
    _transcript_observer = observer


def _write_transcript_entry(entry):
    try:
        os.makedirs(os.path.dirname(TRANSCRIPT_FILE), exist_ok=True)
        with _TRANSCRIPT_LOCK:
            byte_offset = os.path.getsize(TRANSCRIPT_FILE) if os.path.isfile(TRANSCRIPT_FILE) else 0
            with open(TRANSCRIPT_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"telegram_listener.common: failed to append transcript entry ({exc!r}) - continuing anyway", file=sys.stderr)
        return

    if _transcript_observer is not None:
        try:
            _transcript_observer(entry["direction"], entry, byte_offset)
        except Exception as exc:
            # The observer is an integration on the side. The chat itself
            # carries on, but this is said out loud rather than swallowed.
            print(f"telegram_listener.common: transcript observer failed ({exc!r}) - the entry itself was written", file=sys.stderr)


def append_transcript(direction, chat_id, text=None, file_path=None, sender_name=None, sender_id=None,
                      message_id=None, sent_by=None, from_listener=False):
    """Record one message in this chat's history.

    `from_listener` is set by the listener's own send primitives and by
    nothing else. It is what makes "did my own session send this?" an exact
    question rather than a guess: anything written here without it came
    from somewhere else, so a sender that forgets to identify itself is
    treated as out-of-band, which is the harmless direction to be wrong in.
    Anything sending into a chat from outside the listener should pass
    `sent_by` naming itself, since the person reading the chat cannot tell
    a cron alert from a session reply by looking at it.
    """
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "direction": direction,
        "chat_id": chat_id,
    }
    if from_listener:
        entry["from_listener"] = True
    if sent_by:
        entry["sent_by"] = sent_by
    if text:
        entry["text"] = text
    if file_path:
        entry["file"] = file_path
    if sender_name:
        entry["sender_name"] = sender_name
    if sender_id is not None:
        entry["sender_id"] = sender_id
    if message_id is not None:
        entry["message_id"] = message_id

    _write_transcript_entry(entry)


def append_reaction(chat_id, message_id, emoji=None, reactor_name=None, reactor_id=None):
    """Record an inbound `message_reaction` update into the same transcript
    messages go into, so a reader of the chat history sees the reaction
    alongside the message it targets. `emoji` is the reaction now in place,
    or None if the reaction was cleared.
    """
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "direction": "reaction",
        "chat_id": chat_id,
        "message_id": message_id,
    }
    if emoji:
        entry["emoji"] = emoji
    if reactor_name:
        entry["reactor_name"] = reactor_name
    if reactor_id is not None:
        entry["reactor_id"] = reactor_id

    _write_transcript_entry(entry)


def _all_transcript_entries():
    if not os.path.isfile(TRANSCRIPT_FILE):
        return []
    entries = []
    with open(TRANSCRIPT_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def read_transcript(limit=10):
    entries = _all_transcript_entries()
    return entries if limit is None else entries[-limit:]


def read_transcript_since(start_index, chat_id=None):
    """Entries recorded after `start_index`, and the index to resume from.

    `start_index` counts entries already consumed, so the pair acts as a
    durable cursor over the whole transcript. The returned index counts
    every entry, not just the ones matching `chat_id` - otherwise a second
    chat's traffic would silently shift this chat's cursor.
    """
    entries = _all_transcript_entries()
    fresh = entries[start_index:]
    if chat_id is not None:
        fresh = [entry for entry in fresh if entry.get("chat_id") == chat_id]
    return fresh, len(entries)


def pending_confirmations_dir():
    return os.path.join(resolve_data_dir(), "telegram-listener", "pending_confirmations")


def confirmation_path(chat_id, message_id):
    return os.path.join(pending_confirmations_dir(), f"{chat_id}_{message_id}.json")


def _write_confirmation_record(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(record, f)
    os.replace(tmp_path, path)


def create_pending_confirmation(chat_id, message_id, question=None):
    record = {
        "chat_id": chat_id,
        "message_id": message_id,
        "question": question,
        "status": "pending",
        "answer": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "answered_at": None,
        "answered_by": None,
    }
    _write_confirmation_record(confirmation_path(chat_id, message_id), record)
    return record


def record_confirmation_answer(chat_id, message_id, answer, sender_id=None):
    path = confirmation_path(chat_id, message_id)
    if not os.path.isfile(path):
        print(f"telegram_listener.common: no pending confirmation record at {path} for callback answer {answer!r} - ignoring", file=sys.stderr)
        return False
    try:
        with open(path) as f:
            record = json.load(f)
    except Exception as exc:
        print(f"telegram_listener.common: failed to read pending confirmation {path} ({exc!r}) - ignoring", file=sys.stderr)
        return False
    record["status"] = "answered"
    record["answer"] = answer
    record["answered_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record["answered_by"] = sender_id
    _write_confirmation_record(path, record)
    return True


def get_confirmation(chat_id, message_id):
    path = confirmation_path(chat_id, message_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Telegram accepts a reaction only from this fixed set; anything else makes
# setMessageReaction fail with REACTION_INVALID. Variation selectors are
# inconsistent between what a client sends and what the API accepts, so
# comparison strips them and the canonical spelling here is what gets sent.
REACTION_EMOJI = (
    "\u2764", "\U0001F44D", "\U0001F44E", "\U0001F525", "\U0001F970", "\U0001F44F", "\U0001F601",
    "\U0001F914", "\U0001F92F", "\U0001F631", "\U0001F92C", "\U0001F622", "\U0001F389", "\U0001F929",
    "\U0001F92E", "\U0001F4A9", "\U0001F64F", "\U0001F44C", "\U0001F54A", "\U0001F921", "\U0001F971",
    "\U0001F974", "\U0001F60D", "\U0001F433", "\u2764\u200D\U0001F525", "\U0001F31A", "\U0001F32D",
    "\U0001F4AF", "\U0001F923", "\u26A1", "\U0001F34C", "\U0001F3C6", "\U0001F494", "\U0001F928",
    "\U0001F610", "\U0001F353", "\U0001F37E", "\U0001F48B", "\U0001F595", "\U0001F608", "\U0001F634",
    "\U0001F62D", "\U0001F913", "\U0001F47B", "\U0001F468\u200D\U0001F4BB", "\U0001F440",
    "\U0001F383", "\U0001F648", "\U0001F607", "\U0001F628", "\U0001F91D", "\u270D", "\U0001F917",
    "\U0001FAE1", "\U0001F385", "\U0001F384", "\u2603", "\U0001F485", "\U0001F92A", "\U0001F5FF",
    "\U0001F192", "\U0001F498", "\U0001F649", "\U0001F984", "\U0001F618", "\U0001F48A", "\U0001F64A",
    "\U0001F60E", "\U0001F47E", "\U0001F937\u200D\u2642", "\U0001F937", "\U0001F937\u200D\u2640",
    "\U0001F621",
)


def canonical_reaction_emoji(emoji):
    """The spelling of `emoji` Telegram will accept as a reaction, or None if
    it is not a reaction emoji at all."""
    wanted = (emoji or "").replace("\ufe0f", "")
    for allowed in REACTION_EMOJI:
        if allowed.replace("\ufe0f", "") == wanted:
            return allowed
    return None
