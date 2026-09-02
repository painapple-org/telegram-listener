#!/usr/bin/env python3
"""Standalone Telegram long-polling listener with a pluggable dispatch interface.

This is everything genuinely protocol-level: long-polling `getUpdates`,
offset/state persistence, allowed-user filtering, per-chat message batching
(a burst of messages sent within a quiet period collapses into one turn),
attachment download, message-provenance description (replies/forwards/
quotes/polls/etc), and callback-query -> pending-confirmation recording.

What happens with a batched turn is not this module's business - see
`plugin.py`. The concrete example plugin lives in `plugins/claude_agent.py`
and is wired up purely through the `DISPATCH_PLUGIN` environment variable;
nothing in this file imports it or knows it exists.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import requests

from telegram_listener import common
from telegram_listener.plugin import ListenerAPI, Turn, load_plugin

DATA_DIR = common.resolve_data_dir()

STATE_FILE = os.path.join(DATA_DIR, "telegram-listener", "state.json")
RAW_UPDATES_LOG = os.path.join(DATA_DIR, "telegram-listener", "raw_updates.jsonl")
FILES_DIR = os.path.join(DATA_DIR, "telegram-listener", "files")
LOG_FILE = os.path.join(DATA_DIR, "telegram-listener", "listener.log")

MESSAGE_BATCH_QUIET_SECONDS = int(os.environ.get("MESSAGE_BATCH_QUIET_SECONDS", 8))
MESSAGE_BATCH_MAX_WAIT_SECONDS = int(os.environ.get("MESSAGE_BATCH_MAX_WAIT_SECONDS", 120))
MESSAGE_BATCH_MAX_MESSAGES = int(os.environ.get("MESSAGE_BATCH_MAX_MESSAGES", 50))

# The raw-update log is a debugging aid holding full message content, so it
# both grows without bound and accumulates personal data. Cap it and keep
# exactly one previous generation.
RAW_UPDATES_MAX_BYTES = int(os.environ.get("RAW_UPDATES_MAX_BYTES", 50 * 1024 * 1024))

MAX_TELEGRAM_TEXT = 3800

BOT_API_COMPOSE_SERVICE = "telegram-bot-api"

POLL_TIMEOUT = 30
HTTP_TIMEOUT = POLL_TIMEOUT + 10

BUILTIN_BOT_COMMANDS = [
    {"command": "help", "description": "List available commands and what each does"},
]

BOT_ID = None
BOT_USERNAME = None

LOG_LOCK = threading.Lock()


def _env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def log(message):
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    with LOG_LOCK:
        with open(LOG_FILE, "a") as f:
            f.write(f"[pid {os.getpid()}] {common.redact(message)}\n")


# --- Telegram Bot API primitives -------------------------------------------------

def _api_base():
    bot_token = _env("TELEGRAM_BOT_TOKEN")
    return common.resolve_bot_api_base(bot_token)


def _allowed_user_ids():
    ids = {
        int(uid.strip())
        for uid in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
        if uid.strip()
    }
    if not ids:
        raise RuntimeError(
            "TELEGRAM_ALLOWED_USER_IDS is empty - refusing to start a listener that would "
            "allowlist nobody and silently drop every message it ever receives"
        )
    return ids


def _allowed_chat_ids():
    """Optional second gate, on top of the per-sender allowlist. Empty means
    an allowlisted sender may talk to the bot from any chat; set it to
    restrict them to specific chats/groups (see SETUP.md)."""
    return {
        int(cid.strip())
        for cid in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
        if cid.strip()
    }


def _check_response(resp, endpoint):
    """Validate a Bot API response without ever quoting the request URL.

    `requests`' own `raise_for_status` puts the full URL - which carries the
    bot token - into the exception message, and that message reaches log
    files and plugin code. Everything here reports the endpoint name only.
    """
    if resp.status_code >= 400:
        body = getattr(resp, "text", "") or ""
        raise RuntimeError(f"{endpoint} failed: HTTP {resp.status_code} {body[:500]!r}")
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"{endpoint} returned not-ok: {result}")
    return result


def get_updates(offset, api_base):
    params = {"timeout": POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(f"{api_base}/getUpdates", params=params, timeout=HTTP_TIMEOUT)
    return _check_response(resp, "getUpdates")["result"]


def send_message(chat_id, text, api_base):
    try:
        resp = requests.post(
            f"{api_base}/sendMessage",
            data={"chat_id": chat_id, "text": text[:4000] or "(empty response)"},
            timeout=30,
        )
        _check_response(resp, "sendMessage")
    except Exception:
        log(f"send_message FAILED for chat {chat_id}:\n{traceback.format_exc()}")
        raise


def send_document(chat_id, path, api_base, caption=None):
    """Send a local file to a chat. Uncapped in size beyond whatever the
    self-hosted Bot API server itself allows - which is the reason this
    package talks to one instead of api.telegram.org."""
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
    try:
        with open(path, "rb") as f:
            resp = requests.post(
                f"{api_base}/sendDocument", data=data,
                files={"document": (os.path.basename(path), f)}, timeout=600,
            )
        result = _check_response(resp, "sendDocument")
    except Exception:
        log(f"send_document FAILED for chat {chat_id} path {path!r}:\n{traceback.format_exc()}")
        raise
    common.append_transcript("outgoing", chat_id, text=caption or None, file_path=path)
    return result


def send_stream_text(chat_id, text, api_base):
    """Send `text`, splitting across multiple messages if it exceeds Telegram's limit."""
    chunks = [text[i:i + MAX_TELEGRAM_TEXT] for i in range(0, len(text), MAX_TELEGRAM_TEXT)] or [text]
    for chunk in chunks:
        send_message(chat_id, chunk, api_base)


def send_typing_action(chat_id, api_base):
    try:
        resp = requests.post(f"{api_base}/sendChatAction", data={"chat_id": chat_id, "action": "typing"}, timeout=10)
        _check_response(resp, "sendChatAction")
    except Exception as exc:
        log(f"sendChatAction (typing) failed for chat {chat_id}, continuing anyway: {exc!r}")


def set_message_reaction(chat_id, message_id, emoji, api_base):
    try:
        resp = requests.post(
            f"{api_base}/setMessageReaction",
            json={"chat_id": chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": emoji}]},
            timeout=10,
        )
        _check_response(resp, "setMessageReaction")
    except Exception as exc:
        log(f"setMessageReaction failed for chat {chat_id} message {message_id} emoji {emoji!r}: {exc!r}")


def answer_callback_query(callback_query_id, api_base, text=None):
    try:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        resp = requests.post(f"{api_base}/answerCallbackQuery", json=payload, timeout=10)
        _check_response(resp, "answerCallbackQuery")
    except Exception as exc:
        log(f"answerCallbackQuery failed for {callback_query_id}: {exc!r}")


def register_bot_commands(api_base, plugin_commands):
    commands = BUILTIN_BOT_COMMANDS + plugin_commands
    try:
        resp = requests.post(f"{api_base}/setMyCommands", json={"commands": commands}, timeout=10)
        _check_response(resp, "setMyCommands")
    except Exception as exc:
        log(f"setMyCommands failed, continuing anyway (bot will just have no command menu): {exc!r}")


def resolve_bot_identity(api_base):
    global BOT_ID, BOT_USERNAME
    try:
        resp = requests.post(f"{api_base}/getMe", timeout=10)
        me = _check_response(resp, "getMe")["result"]
    except Exception:
        log(f"resolve_bot_identity FAILED - refusing to start:\n{traceback.format_exc()}")
        raise
    BOT_ID = me.get("id")
    BOT_USERNAME = me.get("username")
    log(f"resolve_bot_identity: resolved bot id={BOT_ID} username={BOT_USERNAME!r}")


# --- attachments -------------------------------------------------------------

DOWNLOADABLE_MEDIA = ("animation", "audio", "voice", "video_note", "video", "sticker", "photo", "document")

DEFAULT_MEDIA_EXTENSION = {
    "animation": ".mp4", "audio": ".mp3", "voice": ".ogg", "video_note": ".mp4",
    "video": ".mp4", "sticker": ".webp", "photo": ".jpg", "document": "",
}

MEDIA_EXTENSION_BY_MIME = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
}

MEDIA_LABELS = {
    "animation": "GIF/animation", "audio": "Audio file", "voice": "Voice message",
    "video_note": "Video note (round video message)", "video": "Video", "sticker": "Sticker",
    "photo": "Photo", "document": "Document",
}


def _media_filename(file_id, media, kind):
    name = os.path.basename(media.get("file_name") or "")
    if name:
        return name
    extension = MEDIA_EXTENSION_BY_MIME.get(media.get("mime_type")) or DEFAULT_MEDIA_EXTENSION[kind]
    return f"{file_id}{extension}"


def extract_attachment(message):
    for kind in DOWNLOADABLE_MEDIA:
        media = message.get(kind)
        if not media:
            continue
        if kind == "photo":
            media = media[-1]
        if kind == "sticker":
            extension = ".tgs" if media.get("is_animated") else ".webm" if media.get("is_video") else ".webp"
            return media["file_id"], kind, f"{media['file_id']}{extension}"
        return media["file_id"], kind, _media_filename(media["file_id"], media, kind)
    return None


def bot_api_container_name():
    result = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.service={BOT_API_COMPOSE_SERVICE}", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=30,
    )
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        raise RuntimeError(
            f"no running container found for compose service {BOT_API_COMPOSE_SERVICE!r} "
            f"(docker ps rc={result.returncode}, stderr={result.stderr.strip()!r})"
        )
    return names[0]


def download_and_store_file(file_id, suggested_filename, api_base):
    Path(FILES_DIR).mkdir(parents=True, exist_ok=True)
    dest_path = os.path.join(FILES_DIR, f"{file_id}_{suggested_filename}")
    if os.path.exists(dest_path):
        return dest_path

    resp = requests.get(f"{api_base}/getFile", params={"file_id": file_id}, timeout=30)
    remote_path = _check_response(resp, "getFile")["result"]["file_path"]

    tmp_path = f"{dest_path}.tmp"
    container = bot_api_container_name()
    with open(tmp_path, "wb") as tmp_file:
        proc = subprocess.run(["docker", "exec", container, "cat", remote_path], stdout=tmp_file, stderr=subprocess.PIPE, timeout=600)
    if proc.returncode != 0:
        os.unlink(tmp_path)
        raise RuntimeError(f"reading {remote_path!r} out of container {container} failed (rc={proc.returncode}): {proc.stderr.decode(errors='replace').strip()!r}")
    os.replace(tmp_path, dest_path)
    return dest_path


def render_attachment(kind, attachment_path):
    if not attachment_path:
        return "(none)"
    return f"{MEDIA_LABELS.get(kind, kind)}, saved at {attachment_path}"


# --- message provenance --------------------------------------------------------

def _describe_sender(user):
    if not user:
        return "unknown sender"
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or str(user.get("id"))
    parts = [name]
    if user.get("username"):
        parts.append(f"@{user['username']}")
    if user.get("id"):
        parts.append(f"id {user['id']}")
    return f"{parts[0]} ({', '.join(parts[1:])})" if len(parts) > 1 else parts[0]


def _describe_message_body(message, limit=400):
    body = (message.get("text") or message.get("caption") or "").strip()
    if body:
        snippet = body if len(body) <= limit else body[:limit] + "..."
        return f'"{snippet}"'
    return "(no text)"


def describe_message_provenance(message):
    lines = []

    reply_to = message.get("reply_to_message")
    if reply_to:
        who = "the bot's own earlier message" if (reply_to.get("from") or {}).get("id") == BOT_ID \
            else f"a message from {_describe_sender(reply_to.get('from'))}"
        lines.append(f"This is a reply to {who} (message_id {reply_to.get('message_id')}): {_describe_message_body(reply_to)}")

    quote = message.get("quote")
    if quote:
        lines.append(f'It quotes specifically this part of that message: "{quote.get("text")}"')

    origin = message.get("forward_origin")
    if origin:
        origin_type = origin.get("type")
        if origin_type == "user":
            source = _describe_sender(origin.get("sender_user"))
        elif origin_type == "hidden_user":
            source = f"{origin.get('sender_user_name')} (a user who hides their account on forwards)"
        elif origin_type in ("chat", "channel"):
            source_chat = origin.get("sender_chat") or origin.get("chat") or {}
            source = source_chat.get("title") or source_chat.get("username") or str(source_chat.get("id"))
        else:
            source = f"an origin of type {origin_type!r}"
        lines.append(f"This message is a FORWARD, not the sender's own words - it originally came from {source}.")

    if message.get("edit_date"):
        lines.append("This is an edited version of an earlier message.")

    return "\n".join(f"- {line}" for line in lines) if lines else ""


def _mentions_bot(message):
    text = message.get("text") or message.get("caption") or ""
    entities = (message.get("entities") or []) + (message.get("caption_entities") or [])
    for entity in entities:
        entity_type = entity.get("type")
        if entity_type == "mention" and BOT_USERNAME:
            offset, length = entity.get("offset", 0), entity.get("length", 0)
            if text[offset:offset + length].lower() == f"@{BOT_USERNAME}".lower():
                return True
        elif entity_type == "text_mention" and BOT_ID:
            if (entity.get("user") or {}).get("id") == BOT_ID:
                return True
    return False


def _is_reply_to_bot(message):
    if not BOT_ID:
        return False
    reply_to = message.get("reply_to_message") or {}
    return (reply_to.get("from") or {}).get("id") == BOT_ID


def build_turn_text(sender_name, sender_id, message, text, attachment_kind, attachment_path):
    extras = []

    hints = []
    if _mentions_bot(message):
        hints.append("explicitly @mentioned the bot")
    if _is_reply_to_bot(message):
        hints.append("a reply to one of the bot's own messages")
    if hints:
        extras.append("Addressing signal (a hint, not a requirement): " + ", ".join(hints) + ".")

    if attachment_path:
        extras.append("Attached file, already resolved to a durable local path: " + render_attachment(attachment_kind, attachment_path))

    provenance = describe_message_provenance(message)
    if provenance:
        extras.append("Where this message came from:\n" + provenance)

    body = f"Message from {sender_name} (id {sender_id}):\n{text or '(no text)'}"
    return body + ("\n\n" + "\n\n".join(extras) if extras else "")


def combine_turns(turn_texts):
    if len(turn_texts) == 1:
        return turn_texts[0]
    preamble = (
        f"[{len(turn_texts)} messages arrived together and are delivered as one turn - nobody has "
        f"typed for {MESSAGE_BATCH_QUIET_SECONDS}s since the last one. Reply once, to whatever it "
        f"all adds up to.]\n\n"
    )
    blocks = [f"----- message {i} of {len(turn_texts)} -----\n{text}" for i, text in enumerate(turn_texts, 1)]
    return preamble + "\n\n".join(blocks)


# --- state -------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as exc:
        log(f"state file unreadable ({exc!r}), starting as if fresh")
        return {}


def save_state(state):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    tmp_file = f"{STATE_FILE}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(state, f)
    os.replace(tmp_file, STATE_FILE)


def store_raw_update(update):
    """Append one update to the raw-update log, rotating at
    RAW_UPDATES_MAX_BYTES. Only ever called for updates that already passed
    the allowlist - a stranger's message content is never written to disk.

    A failure here is loud but never fatal: this log is a debugging aid, and
    a full disk or a bad permission on it must not take the listener down
    for every user it is currently serving.
    """
    try:
        Path(RAW_UPDATES_LOG).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(RAW_UPDATES_LOG) and os.path.getsize(RAW_UPDATES_LOG) >= RAW_UPDATES_MAX_BYTES:
            os.replace(RAW_UPDATES_LOG, f"{RAW_UPDATES_LOG}.1")
        with open(RAW_UPDATES_LOG, "a") as f:
            f.write(json.dumps(update) + "\n")
    except Exception:
        log(f"store_raw_update FAILED for update {update.get('update_id')}, continuing:\n{traceback.format_exc()}")


# --- per-chat batching ---------------------------------------------------------

class ChatBatcher:
    """Collapses a burst of messages from one chat into a single dispatched turn.

    Sender identity travels with each queued message rather than being
    bound once when the batcher is created: in a group chat, consecutive
    turns come from different people, and a batcher lives for the whole
    process.
    """

    def __init__(self, chat_id, dispatch):
        self.chat_id = chat_id
        self.dispatch = dispatch
        self.inbox = []
        self.batch_started_at = 0.0
        self.timer_task = None
        self.dispatch_tasks = set()

    async def queue(self, message_id, turn_text, sender_name, sender_id):
        if not self.inbox:
            self.batch_started_at = time.time()
        self.inbox.append((message_id, turn_text, sender_name, sender_id))
        if len(self.inbox) >= MESSAGE_BATCH_MAX_MESSAGES:
            log(f"chat {self.chat_id}: batch hit {MESSAGE_BATCH_MAX_MESSAGES} messages, delivering without waiting out the quiet period")
            self._cancel_timer()
            self.flush()
            return
        self._restart_timer()

    def _cancel_timer(self):
        if self.timer_task is not None and not self.timer_task.done():
            self.timer_task.cancel()
        self.timer_task = None

    def _restart_timer(self):
        self._cancel_timer()
        self.timer_task = asyncio.create_task(self._flush_when_quiet())

    async def _flush_when_quiet(self):
        delay = MESSAGE_BATCH_QUIET_SECONDS
        if self.batch_started_at:
            delay = min(delay, max(0.0, self.batch_started_at + MESSAGE_BATCH_MAX_WAIT_SECONDS - time.time()))
        await asyncio.sleep(delay)
        self.timer_task = None
        self.flush()

    def flush(self):
        """Hand the current batch to the dispatch layer, in a task of its own.

        Deliberately not awaited from the quiet-period timer: a later
        message restarts that timer, which cancels it, and a dispatch
        running inside it would be cancelled along with it - killing a turn
        already in flight halfway through, leaving the plugin's own
        per-chat state wherever the exception landed.
        """
        batch, self.inbox = self.inbox, []
        self.batch_started_at = 0.0
        if not batch:
            return
        task = asyncio.create_task(self._dispatch_batch(batch))
        # Hold a reference until it finishes; asyncio only weakly
        # references running tasks and will otherwise collect one mid-turn.
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    async def _dispatch_batch(self, batch):
        message_ids = [mid for mid, _, _, _ in batch]
        text = combine_turns([t for _, t, _, _ in batch])
        # The most recent message in the batch is the one being replied to,
        # so its sender is the turn's sender.
        _, _, sender_name, sender_id = batch[-1]
        log(f"chat {self.chat_id}: delivering {len(batch)} message(s) as one turn")
        try:
            await self.dispatch(self.chat_id, message_ids, text, sender_name, sender_id)
        except Exception:
            log(f"chat {self.chat_id}: dispatch failed:\n{traceback.format_exc()}")


class Listener:
    def __init__(self, plugin, api_base=None, allowed_user_ids=None, allowed_chat_ids=None):
        self.plugin = plugin
        self.api_base = api_base or _api_base()
        self.allowed_user_ids = allowed_user_ids if allowed_user_ids is not None else _allowed_user_ids()
        self.allowed_chat_ids = allowed_chat_ids if allowed_chat_ids is not None else _allowed_chat_ids()
        self._batchers = {}
        self._chat_locks = {}
        self._turn_locks = {}

    def _api(self):
        # Every send primitive is handed over as a coroutine function: the
        # underlying calls are blocking `requests` calls, and running one
        # inline on the event loop would stall the poll loop and every
        # other chat for its full duration.
        return ListenerAPI(
            send_message=lambda chat_id, text: asyncio.to_thread(send_stream_text, chat_id, text, self.api_base),
            send_typing=lambda chat_id: asyncio.to_thread(send_typing_action, chat_id, self.api_base),
            set_reaction=lambda chat_id, message_id, emoji: asyncio.to_thread(
                set_message_reaction, chat_id, message_id, emoji, self.api_base),
            send_document=lambda chat_id, path, caption=None: asyncio.to_thread(
                send_document, chat_id, path, self.api_base, caption),
            log=log,
        )

    def _batcher(self, chat_id):
        batcher = self._batchers.get(chat_id)
        if batcher is None:
            batcher = ChatBatcher(chat_id, self._dispatch_turn)
            self._batchers[chat_id] = batcher
        return batcher

    def _chat_lock(self, chat_id):
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    def _turn_lock(self, chat_id):
        lock = self._turn_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[chat_id] = lock
        return lock

    def _allowed(self, sender_id, chat_id):
        """The complete access-control gate: an allowlisted sender, and - if
        TELEGRAM_ALLOWED_CHAT_IDS is set - an allowlisted chat. Returns
        (allowed, reason-if-not)."""
        if sender_id not in self.allowed_user_ids:
            return False, f"disallowed user id {sender_id}"
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            return False, f"allowlisted user {sender_id} but disallowed chat id {chat_id}"
        return True, ""

    async def _dispatch_turn(self, chat_id, message_ids, text, sender_name, sender_id):
        # One turn at a time per chat. A plugin holds per-chat state (an
        # agent session, a subprocess) that concurrent turns would corrupt,
        # and a turn can outlast the next batch's quiet period.
        async with self._turn_lock(chat_id):
            turn = Turn(chat_id=chat_id, message_ids=message_ids, text=text,
                        sender_name=sender_name, sender_id=sender_id)
            try:
                await self.plugin.handle_turn(turn, self._api())
            except Exception:
                log(f"chat {chat_id}: plugin.handle_turn failed:\n{traceback.format_exc()}")
                reaction = "\U0001F44E"
            else:
                reaction = "\U0001F44D"
            for message_id in message_ids:
                await asyncio.to_thread(set_message_reaction, chat_id, message_id, reaction, self.api_base)

    def handle_callback_query(self, callback_query):
        query_id = callback_query["id"]
        sender = callback_query.get("from") or {}
        sender_id = sender.get("id")
        data = callback_query.get("data", "")
        message = callback_query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")

        allowed, reason = self._allowed(sender_id, chat_id)
        if not allowed:
            log(f"DROPPED callback_query: {reason}, data={data!r}")
            answer_callback_query(query_id, self.api_base, text="Not authorized.")
            return

        if chat_id is None or message_id is None:
            log(f"callback_query missing chat/message id, can't record an answer: {callback_query!r}")
            answer_callback_query(query_id, self.api_base)
            return

        recorded = common.record_confirmation_answer(chat_id, message_id, data, sender_id=sender_id)
        log(f"callback_query: data={data!r} chat={chat_id} message_id={message_id} recorded={recorded}")
        answer_callback_query(query_id, self.api_base, text="Recorded." if recorded else None)

    async def handle_message(self, message):
        sender = message.get("from") or {}
        sender_id = sender.get("id")
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        sender_name = sender.get("first_name") or sender.get("username") or str(sender_id)

        allowed, reason = self._allowed(sender_id, chat_id)
        if not allowed:
            log(f"DROPPED: message from {reason} ({sender_name!r}, chat {chat_id})")
            return

        text = message.get("text") or message.get("caption") or ""
        stripped = text.strip()

        if stripped.startswith("/"):
            command = stripped.split()[0][1:].split("@", 1)[0].lower()
            argument = stripped.split(maxsplit=1)[1].strip() if len(stripped.split(maxsplit=1)) > 1 else ""
            if command == "help":
                await asyncio.to_thread(set_message_reaction, chat_id, message_id, "\U0001F440", self.api_base)
                lines = [f"/{c['command']} - {c['description']}" for c in BUILTIN_BOT_COMMANDS + self.plugin.describe_commands()]
                await asyncio.to_thread(send_message, chat_id, "\n".join(lines), self.api_base)
                return
            handled = await self.plugin.handle_command(chat_id, message_id, command, argument, self._api())
            if handled:
                return
            # Not a builtin, and the plugin didn't recognize it either -
            # fall through and hand it to handle_turn as ordinary text,
            # same as any other message.

        attachment_path = ""
        attachment_kind = ""
        try:
            attachment = extract_attachment(message)
            if attachment:
                file_id, attachment_kind, filename = attachment
                attachment_path = await asyncio.to_thread(download_and_store_file, file_id, filename, self.api_base)
        except Exception as exc:
            log(f"chat {chat_id}: attachment download failed: {exc!r}")
            await asyncio.to_thread(send_message, chat_id, "Your attachment came in, but I couldn't fetch it.", self.api_base)

        if not text and not attachment_path:
            if not describe_message_provenance(message):
                log(f"chat {chat_id}: message with no text/caption/attachment and nothing else describable - skipping")
                return
            text = "[No text and no downloadable attachment - see provenance below]"

        common.append_transcript("incoming", chat_id, text=text or None, file_path=attachment_path or None, sender_name=sender_name, sender_id=sender_id)

        turn_text = build_turn_text(sender_name, sender_id, message, text, attachment_kind, attachment_path)
        log(f"ALLOWED: queued for chat {chat_id} - sender={sender_name} ({sender_id}) text={text!r} attachment={attachment_kind or 'none'}")
        await self._batcher(chat_id).queue(message_id, turn_text, sender_name, sender_id)

    async def handle_update(self, update):
        callback_query = update.get("callback_query")
        if callback_query:
            await asyncio.to_thread(self.handle_callback_query, callback_query)
            return

        if update.get("edited_message") and not update.get("message"):
            # An edit is not a new turn. Re-running the agent because
            # somebody fixed a typo means paying for, and acting on, the
            # same message twice.
            log(f"update {update['update_id']}: edited message, not re-running the turn")
            return

        message = update.get("message")
        if not message:
            log(f"update {update['update_id']}: no message/edited_message/callback_query field, skipping")
            return

        sender_id = (message.get("from") or {}).get("id")
        chat_id = message["chat"]["id"]
        allowed, reason = self._allowed(sender_id, chat_id)
        if not allowed:
            # Gate before anything is written to disk or any per-chat state
            # is allocated, so an unauthorized sender can neither get their
            # message content persisted nor grow this process's memory.
            log(f"DROPPED update {update['update_id']}: {reason}")
            return

        await asyncio.to_thread(store_raw_update, update)

        async with self._chat_lock(chat_id):
            await self.handle_message(message)

    async def _handle_update_safe(self, update):
        try:
            await self.handle_update(update)
        except Exception:
            log(f"update {update['update_id']}: unhandled error while processing:\n{traceback.format_exc()}")

    async def run(self):
        log("telegram listener starting")
        await asyncio.to_thread(register_bot_commands, self.api_base, self.plugin.describe_commands())
        await asyncio.to_thread(resolve_bot_identity, self.api_base)
        state = load_state()
        last_update_id = state.get("last_update_id")
        offset = (last_update_id + 1) if last_update_id is not None else None
        log(f"resuming from offset={offset!r} (last_update_id={last_update_id!r})")

        while True:
            try:
                updates = await asyncio.to_thread(get_updates, offset, self.api_base)
            except Exception as exc:
                log(f"getUpdates failed, backing off 5s: {exc!r}")
                await asyncio.sleep(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                save_state({"last_update_id": update["update_id"]})
                asyncio.create_task(self._handle_update_safe(update))


def main():
    plugin = load_plugin()
    # Construct the listener before the poll loop starts, so a missing token
    # or an empty allowlist fails immediately and visibly rather than after
    # the service reports itself active.
    listener = Listener(plugin)
    try:
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
