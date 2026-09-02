"""Minimal shared helpers the listener/protocol layer actually needs.

This is a deliberately small subset of what `spoor`'s own
`scripts/shared/telegram_common.py` provides: bot-API base resolution,
transcript logging, and the pending-confirmation record used by callback
queries. Spoor's own `activity_log`/dashboard-event plumbing does not live
here - that's a spoor-specific integration, not something a generic
listener needs. Anything downstream that wants an event notification gets
it through the optional `ListenerAPI.on_event` callback instead (see
`plugin.py`), never through a hard import of spoor's own module.
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


def append_transcript(direction, chat_id, text=None, file_path=None, sender_name=None, sender_id=None):
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "direction": direction,
        "chat_id": chat_id,
    }
    if text:
        entry["text"] = text
    if file_path:
        entry["file"] = file_path
    if sender_name:
        entry["sender_name"] = sender_name
    if sender_id is not None:
        entry["sender_id"] = sender_id

    try:
        os.makedirs(os.path.dirname(TRANSCRIPT_FILE), exist_ok=True)
        with _TRANSCRIPT_LOCK:
            with open(TRANSCRIPT_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"telegram_listener.common: failed to append transcript entry ({exc!r}) - continuing anyway", file=sys.stderr)


def read_transcript(limit=10):
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
    return entries if limit is None else entries[-limit:]


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
