#!/usr/bin/env python3
"""Standalone Telegram long-polling listener with a pluggable dispatch interface.

This is everything genuinely protocol-level: long-polling `getUpdates`,
offset/state persistence, allowed-user filtering, per-chat message batching
(a burst of messages sent within a quiet period collapses into one turn),
attachment download and optional speech-to-text transcription, full
message-content description (replies/forwards/quotes/polls/locations/
contacts/albums/chat events/...), inbound reaction updates, and
callback-query -> pending-confirmation recording.

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

# How long a newly batched turn waits behind a turn already in flight for
# the same chat before the listener asks the plugin to interrupt that turn
# (plugin.interrupt_turn - optional; without it the new turn simply waits).
TURN_YIELD_GRACE_SECONDS = int(os.environ.get("TURN_YIELD_GRACE_SECONDS", 15))

# `/sleep` with no argument.
SLEEP_DEFAULT_SECONDS = int(os.environ.get("SLEEP_DEFAULT_SECONDS", 15 * 60))

# Telegram's typing indicator expires after about five seconds, so holding
# it up for a whole turn means re-sending it on a timer.
TYPING_ACTION_INTERVAL = 4

# At most one plugin.live_context() call per chat per interval. The hook is
# there to fold fresh out-of-band state into a turn, not to be re-run on
# every message of a burst.
LIVE_CONTEXT_REFRESH_INTERVAL_SECONDS = int(os.environ.get("LIVE_CONTEXT_REFRESH_INTERVAL_SECONDS", 10 * 60))

# The raw-update log is a debugging aid holding full message content, so it
# both grows without bound and accumulates personal data. Cap it and keep
# exactly one previous generation.
RAW_UPDATES_MAX_BYTES = int(os.environ.get("RAW_UPDATES_MAX_BYTES", 50 * 1024 * 1024))

MAX_TELEGRAM_TEXT = 3800

BOT_API_COMPOSE_SERVICE = "telegram-bot-api"

POLL_TIMEOUT = 30
HTTP_TIMEOUT = POLL_TIMEOUT + 10

# Reactions the listener itself puts on an incoming message to show, with
# no notification noise, where that message currently stands.
REACTION_RECEIVED = "\U0001F440"
REACTION_SLEEPING = "\U0001F634"
REACTION_DONE = "\U0001F44D"
REACTION_FAILED = "\U0001F44E"

# Telegram only delivers message_reaction/message_reaction_count updates
# when allowed_updates names them explicitly - unlike every other update
# type here, they are opt-in rather than on by default. Listing
# allowed_updates at all means every type still wanted has to be named.
ALLOWED_UPDATE_TYPES = ["message", "edited_message", "callback_query", "message_reaction"]

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
    params = {"timeout": POLL_TIMEOUT, "allowed_updates": json.dumps(ALLOWED_UPDATE_TYPES)}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(f"{api_base}/getUpdates", params=params, timeout=HTTP_TIMEOUT)
    return _check_response(resp, "getUpdates")["result"]


def _reply_parameters(reply_to_message_id):
    """Telegram rejects the whole send if the message being replied to has
    since been deleted, unless allow_sending_without_reply says otherwise -
    and a reply that fails is worse than a reply that lost its threading."""
    if reply_to_message_id is None:
        return None
    return json.dumps({"message_id": reply_to_message_id, "allow_sending_without_reply": True})


def send_message(chat_id, text, api_base, reply_to_message_id=None, disable_notification=False):
    data = {
        "chat_id": chat_id,
        "text": text[:4000] or "(empty response)",
        "disable_notification": "true" if disable_notification else "false",
    }
    reply_parameters = _reply_parameters(reply_to_message_id)
    if reply_parameters:
        data["reply_parameters"] = reply_parameters
    try:
        resp = requests.post(f"{api_base}/sendMessage", data=data, timeout=30)
        result = _check_response(resp, "sendMessage")
    except Exception:
        log(f"send_message FAILED for chat {chat_id}:\n{traceback.format_exc()}")
        raise
    message_id = (result.get("result") or {}).get("message_id")
    common.append_transcript("outgoing", chat_id, text=text, message_id=message_id)
    return message_id


def edit_message(chat_id, message_id, text, api_base):
    """Replace the text of a message this bot already sent. Returns False if
    Telegram refused for any reason other than the text being unchanged -
    which is not a failure, just a no-op."""
    data = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
    try:
        resp = requests.post(f"{api_base}/editMessageText", data=data, timeout=30)
    except Exception as exc:
        log(f"editMessageText failed for chat {chat_id} message {message_id}: {exc!r}")
        return False
    if resp.status_code >= 400:
        try:
            description = resp.json().get("description", "")
        except ValueError:
            description = ""
        if "message is not modified" in description.lower():
            return True
        log(f"editMessageText refused for chat {chat_id} message {message_id}: {description or resp.status_code}")
        return False
    common.append_transcript("outgoing", chat_id, text=text, message_id=message_id)
    return True


def delete_message(chat_id, message_id, api_base):
    try:
        resp = requests.post(
            f"{api_base}/deleteMessage",
            data={"chat_id": chat_id, "message_id": message_id}, timeout=15,
        )
        _check_response(resp, "deleteMessage")
    except Exception as exc:
        log(f"deleteMessage failed for chat {chat_id} message {message_id}: {exc!r}")


def send_document(chat_id, path, api_base, caption=None, reply_to_message_id=None):
    """Send a local file to a chat. Uncapped in size beyond whatever the
    self-hosted Bot API server itself allows - which is the reason this
    package talks to one instead of api.telegram.org."""
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
    reply_parameters = _reply_parameters(reply_to_message_id)
    if reply_parameters:
        data["reply_parameters"] = reply_parameters
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
    common.append_transcript(
        "outgoing", chat_id, text=caption or None, file_path=path,
        message_id=(result.get("result") or {}).get("message_id"),
    )
    return result


def send_stream_text(chat_id, text, api_base, reply_to_message_id=None, disable_notification=False):
    """Send `text`, splitting across multiple messages if it exceeds Telegram's
    limit. Only the first chunk carries the reply threading; returns the
    message_id of the last chunk actually sent."""
    chunks = [text[i:i + MAX_TELEGRAM_TEXT] for i in range(0, len(text), MAX_TELEGRAM_TEXT)] or [text]
    last_id = None
    for index, chunk in enumerate(chunks):
        last_id = send_message(
            chat_id, chunk, api_base,
            reply_to_message_id=reply_to_message_id if index == 0 else None,
            disable_notification=disable_notification,
        )
    return last_id


def send_typing_action(chat_id, api_base):
    try:
        resp = requests.post(f"{api_base}/sendChatAction", data={"chat_id": chat_id, "action": "typing"}, timeout=10)
        _check_response(resp, "sendChatAction")
    except Exception as exc:
        log(f"sendChatAction (typing) failed for chat {chat_id}, continuing anyway: {exc!r}")


def set_message_reaction(chat_id, message_id, emoji, api_base):
    canonical = common.canonical_reaction_emoji(emoji)
    if canonical is None:
        log(f"setMessageReaction skipped: {emoji!r} is not one of the emoji Telegram accepts as a reaction")
        return
    try:
        resp = requests.post(
            f"{api_base}/setMessageReaction",
            json={"chat_id": chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": canonical}]},
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


def report_failure(chat_id, what, api_base, exc=None):
    """Say in the chat that something broke, as well as in the log.

    A log file nobody is tailing is silence, and quietly broken is worse
    than loudly broken: the people in this chat are the ones who have to
    decide whether to say it again, wait, or go and look.
    """
    detail = f"\n{type(exc).__name__}: {exc}" if exc is not None else ""
    log(f"chat {chat_id}: REPORTED TO CHAT - {what}{detail}\n{traceback.format_exc()}")
    try:
        send_message(chat_id, f"⚠️ {what}{detail}\n(see {LOG_FILE})", api_base)
    except Exception as send_exc:
        log(f"chat {chat_id}: reporting that failure to the chat ALSO failed: {send_exc!r}")


def register_bot_commands(api_base, commands):
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


# --- speech to text / text to speech -------------------------------------------

# Both are optional and both are off unless an endpoint is configured. The
# expected shape is the OpenAI audio API (`/v1/audio/transcriptions` and
# `/v1/audio/speech`), which whisper.cpp's server, faster-whisper-server,
# openedai-speech, Piper's OpenAI shim and the OpenAI API itself all speak -
# so this works against a box on your own network or a hosted provider
# without the listener caring which.
STT_ENDPOINT = os.environ.get("TELEGRAM_STT_ENDPOINT", "")
STT_MODEL = os.environ.get("TELEGRAM_STT_MODEL", "whisper-1")
STT_TIMEOUT = int(os.environ.get("TELEGRAM_STT_TIMEOUT", 900))

TTS_ENDPOINT = os.environ.get("TELEGRAM_TTS_ENDPOINT", "")
TTS_MODEL = os.environ.get("TELEGRAM_TTS_MODEL", "tts-1")
TTS_VOICE = os.environ.get("TELEGRAM_TTS_VOICE", "alloy")
TTS_TIMEOUT = int(os.environ.get("TELEGRAM_TTS_TIMEOUT", 120))

# Transcribing an hour of video is slow and rarely what anyone wanted when
# they dropped a large file into a chat; above this the raw file is handed
# over as a path instead.
TRANSCRIBE_MAX_BYTES = int(os.environ.get("TRANSCRIBE_MAX_BYTES", 200 * 1024 * 1024))

TRANSCRIBABLE_KINDS = ("voice", "audio", "video_note", "video")

VOICE_UNAVAILABLE_NOTE = "\n\n(voice unavailable right now, sending as text)"


def transcription_enabled():
    return bool(STT_ENDPOINT)


def tts_enabled():
    return bool(TTS_ENDPOINT)


def transcribe_media(local_path):
    with open(local_path, "rb") as f:
        resp = requests.post(
            STT_ENDPOINT, files={"file": f}, data={"model": STT_MODEL}, timeout=STT_TIMEOUT,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"speech-to-text failed: HTTP {resp.status_code} {(resp.text or '')[:500]!r}")
    text = (resp.json().get("text") or "").strip()
    if not text:
        log(f"transcribe_media: the STT response for {local_path!r} had no usable 'text' field")
        return None
    return text


def store_media_transcript(file_id, transcript):
    Path(FILES_DIR).mkdir(parents=True, exist_ok=True)
    dest_path = os.path.join(FILES_DIR, f"{file_id}_transcript.txt")
    tmp_path = f"{dest_path}.tmp"
    with open(tmp_path, "w") as f:
        f.write(transcript)
    os.replace(tmp_path, dest_path)
    return dest_path


def synthesize_speech(text):
    """Opus-encoded bytes for `text`, or None if no TTS endpoint is
    configured or the endpoint didn't answer."""
    if not tts_enabled():
        return None
    try:
        resp = requests.post(
            TTS_ENDPOINT,
            json={"model": TTS_MODEL, "input": text, "voice": TTS_VOICE, "response_format": "opus"},
            timeout=TTS_TIMEOUT,
        )
        if resp.status_code >= 400:
            log(f"synthesize_speech: HTTP {resp.status_code} {(resp.text or '')[:500]!r}")
            return None
        return resp.content
    except Exception as exc:
        log(f"synthesize_speech failed: {exc!r}")
        return None


def send_voice_message(chat_id, text, api_base, reply_to_message_id=None):
    """Speak `text` into the chat. Falls back to sending it as text (with a
    note saying so) rather than going quiet when TTS is unavailable; returns
    whether the voice message itself went out."""
    ogg_bytes = synthesize_speech(text)
    if ogg_bytes is not None:
        data = {"chat_id": chat_id}
        reply_parameters = _reply_parameters(reply_to_message_id)
        if reply_parameters:
            data["reply_parameters"] = reply_parameters
        try:
            resp = requests.post(
                f"{api_base}/sendVoice", data=data,
                files={"voice": ("voice.ogg", ogg_bytes)}, timeout=120,
            )
            result = _check_response(resp, "sendVoice")
            common.append_transcript(
                "outgoing", chat_id, text="[voice message]",
                message_id=(result.get("result") or {}).get("message_id"),
            )
            return True
        except Exception as exc:
            log(f"send_voice_message: sendVoice failed for chat {chat_id}: {exc!r} - falling back to text")

    send_stream_text(chat_id, text + VOICE_UNAVAILABLE_NOTE, api_base,
                     reply_to_message_id=reply_to_message_id)
    return False


# --- attachments -------------------------------------------------------------

DOWNLOADABLE_MEDIA = (
    "animation", "audio", "voice", "video_note", "video",
    "sticker", "live_photo", "photo", "document",
)

DEFAULT_MEDIA_EXTENSION = {
    "animation": ".mp4", "audio": ".mp3", "voice": ".ogg", "video_note": ".mp4",
    "video": ".mp4", "sticker": ".webp", "live_photo": ".mov", "photo": ".jpg",
    "document": "",
}

MEDIA_EXTENSION_BY_MIME = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/m4a": ".m4a", "audio/mp4": ".m4a",
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/flac": ".flac", "audio/aac": ".aac",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv",
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
}

MEDIA_LABELS = {
    "animation": "GIF/animation", "audio": "Audio file", "voice": "Voice message",
    "video_note": "Video note (round video message)", "video": "Video", "sticker": "Sticker",
    "live_photo": "Live photo", "photo": "Photo", "document": "Document",
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


def eager_resolve_attachment(message, api_base):
    """Start the download as soon as the update arrives, rather than when
    the batch is finally dispatched.

    A file lands on disk while the quiet period is still running, so by the
    time anything wants to read it the wait is already over. Failure here is
    not reported: `handle_message` downloads again (hitting the on-disk copy
    when this worked) and reports properly if it still cannot.
    """
    try:
        attachment = extract_attachment(message)
        if attachment:
            file_id, _kind, filename = attachment
            download_and_store_file(file_id, filename, api_base)
    except Exception as exc:
        log(f"eager_resolve_attachment failed (handle_message will retry and report): {exc!r}")


def render_attachment(kind, attachment_path):
    if not attachment_path:
        return "(none)"
    return f"{MEDIA_LABELS.get(kind, kind)}, saved at {attachment_path}"


# --- message content description -----------------------------------------------

NON_FILE_CONTENT_KINDS = ("location", "venue", "contact", "poll", "dice", "checklist", "story", "game")

# Every Bot API message field this module either handles or has decided it
# does not need to. Anything outside it is reported as unhandled rather than
# silently dropped - see `describe_unknown_fields`.
KNOWN_MESSAGE_KEYS = frozenset(DOWNLOADABLE_MEDIA) | frozenset(NON_FILE_CONTENT_KINDS) | {
    "message_id", "message_thread_id", "from", "sender_chat", "chat", "date",
    "is_topic_message", "author_signature", "business_connection_id",
    "text", "entities", "caption", "caption_entities", "link_preview_options",
    "show_caption_above_media",
    "reply_to_message", "forward_origin", "forward_from", "forward_from_chat",
    "forward_from_message_id", "forward_signature", "forward_sender_name", "forward_date",
    "is_automatic_forward", "external_reply", "quote", "reply_to_story", "via_bot", "edit_date",
    "media_group_id", "has_media_spoiler",
    "pinned_message", "new_chat_members", "left_chat_member", "new_chat_title",
    "new_chat_photo", "delete_chat_photo",
    "has_protected_content", "is_from_offline", "effect_id", "reply_markup",
}


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
    for kind in DOWNLOADABLE_MEDIA + NON_FILE_CONTENT_KINDS:
        if kind in message:
            return f"(no text - a {kind})"
    return "(no text)"


def _format_timestamp(unix_seconds):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(unix_seconds))


def _maps_link(latitude, longitude):
    return f"https://www.google.com/maps?q={latitude},{longitude}"


def _describe_location(location, prefix="Shared location"):
    line = (
        f"{prefix}: latitude {location.get('latitude')}, longitude {location.get('longitude')} "
        f"({_maps_link(location.get('latitude'), location.get('longitude'))})"
    )
    if location.get("live_period"):
        line += f" - this is a LIVE location, updating for the next {location['live_period']}s"
    return line + "."


def _describe_contact(contact):
    name = " ".join(p for p in (contact.get("first_name"), contact.get("last_name")) if p)
    line = f"Shared contact card: {name or '(no name)'}, phone {contact.get('phone_number') or '(none given)'}"
    if contact.get("user_id"):
        line += f", Telegram user id {contact['user_id']}"
    if contact.get("vcard"):
        line += f" (a full vCard is attached to the message; the raw update in {RAW_UPDATES_LOG} has it verbatim)"
    return line + "."


def _describe_poll(poll):
    options = " | ".join(option.get("text", "") for option in poll.get("options") or [])
    shape = "quiz" if poll.get("type") == "quiz" else "poll"
    line = f'Sent a {shape}: "{poll.get("question")}" with options: {options or "(none)"}'
    traits = []
    if poll.get("allows_multiple_answers"):
        traits.append("multiple answers allowed")
    if poll.get("is_anonymous"):
        traits.append("anonymous")
    if poll.get("is_closed"):
        traits.append("already closed")
    if traits:
        line += f" ({', '.join(traits)})"
    correct = poll.get("correct_option_ids")
    if correct:
        line += f". Correct option index/indices: {', '.join(str(i) for i in correct)}"
    if poll.get("explanation"):
        line += f'. Explanation: "{poll["explanation"]}"'
    return line + "."


def _describe_checklist(checklist):
    tasks = "; ".join(task.get("text", "") for task in checklist.get("tasks") or [])
    return f'Sent a checklist titled "{checklist.get("title")}" with tasks: {tasks or "(none)"}.'


def describe_nonfile_content(message):
    """Everything a message carries that isn't text and isn't a downloadable
    file: a location, a poll, a contact card, a chat event, album membership.

    Without this the whole message reaches a plugin as "(no text)" - which
    is not an absence of content, it is content silently thrown away.
    """
    lines = []

    if "venue" in message:
        venue = message["venue"]
        location = venue.get("location") or {}
        lines.append(
            f"Shared venue: {venue.get('title')} - {venue.get('address')} "
            f"(latitude {location.get('latitude')}, longitude {location.get('longitude')}, "
            f"{_maps_link(location.get('latitude'), location.get('longitude'))})."
        )
    elif "location" in message:
        lines.append(_describe_location(message["location"]))

    if "contact" in message:
        lines.append(_describe_contact(message["contact"]))
    if "poll" in message:
        lines.append(_describe_poll(message["poll"]))
    if "checklist" in message:
        lines.append(_describe_checklist(message["checklist"]))
    if "dice" in message:
        dice = message["dice"]
        lines.append(
            f"Threw a Telegram dice/animated emoji {dice.get('emoji')} - it landed on {dice.get('value')}. "
            f"This is a bit of fun, not a piece of work."
        )
    if "story" in message:
        story = message["story"]
        lines.append(
            f"Shared a Telegram story (story id {story.get('id')} from chat "
            f"{(story.get('chat') or {}).get('id')}) - stories aren't downloadable through the Bot API, "
            f"so only this reference exists here."
        )
    if "game" in message:
        lines.append(f"Sent the Telegram game {(message['game'] or {}).get('title')!r}.")

    if "sticker" in message:
        sticker = message["sticker"]
        parts = [f"emoji {sticker['emoji']}"] if sticker.get("emoji") else []
        if sticker.get("set_name"):
            parts.append(f"from the set {sticker['set_name']}")
        lines.append(
            f"This is a sticker ({', '.join(parts) or 'no emoji or set name'}) - the underlying file is "
            f"downloaded and attached, but a sticker is usually an expressive reaction rather than content "
            f"to act on."
        )

    if message.get("has_media_spoiler"):
        lines.append("The attached media was sent spoiler-blurred (hidden until tapped).")

    if message.get("media_group_id"):
        lines.append(
            f"This is one item of an album (media_group_id {message['media_group_id']}) - Telegram delivers "
            f"each item as its own message, so the sibling items arrive as their own separate turns."
        )

    if "pinned_message" in message:
        lines.append(f"Pinned a message in this chat: {_describe_message_body(message['pinned_message'])}")
    if "new_chat_members" in message:
        who = ", ".join(_describe_sender(user) for user in message["new_chat_members"])
        lines.append(f"Added to this chat: {who}.")
    if "left_chat_member" in message:
        lines.append(f"Left (or was removed from) this chat: {_describe_sender(message['left_chat_member'])}.")
    if "new_chat_title" in message:
        lines.append(f"Renamed this chat to \"{message['new_chat_title']}\".")
    if "new_chat_photo" in message:
        lines.append("Set a new photo for this chat.")
    if message.get("delete_chat_photo"):
        lines.append("Removed this chat's photo.")

    return lines


def describe_unknown_fields(message):
    """Say out loud when a message carries a Bot API field this listener has
    no handling for. Telegram keeps adding message types; the alternative to
    naming them is a message that quietly arrives as less than it was."""
    unknown = sorted(key for key in message if key not in KNOWN_MESSAGE_KEYS)
    if not unknown:
        return []
    return [
        f"This message also carries Bot API field(s) this listener doesn't specifically handle: "
        f"{', '.join(unknown)}. The full raw update is in {RAW_UPDATES_LOG} "
        f"(match on message_id {message.get('message_id')}) - read it there if it looks relevant, "
        f"and say so, since it means a message type is going partly unhandled."
    ]


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

    external_reply = message.get("external_reply")
    if external_reply:
        origin_chat = external_reply.get("chat") or {}
        lines.append(
            f"It replies to a message in a DIFFERENT chat "
            f"({origin_chat.get('title') or origin_chat.get('id')}, message_id "
            f"{external_reply.get('message_id')}), which this listener can't read - only this reference to it."
        )

    reply_to_story = message.get("reply_to_story")
    if reply_to_story:
        lines.append(f"It is a reply to a Telegram story (story id {reply_to_story.get('id')}).")

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
            if origin.get("author_signature"):
                source = f"{source}, signed {origin['author_signature']}"
        else:
            source = f"an origin of type {origin_type!r}"
        when = f", originally sent {_format_timestamp(origin['date'])}" if origin.get("date") else ""
        lines.append(f"This message is a FORWARD, not the sender's own words - it originally came from {source}{when}.")

    if message.get("edit_date"):
        lines.append(f"This is an edited version of an earlier message (edited {_format_timestamp(message['edit_date'])}).")

    via_bot = message.get("via_bot")
    if via_bot:
        lines.append(f"It was composed through the inline bot @{via_bot.get('username')}, not typed directly.")

    lines.extend(describe_nonfile_content(message))
    lines.extend(describe_unknown_fields(message))

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


def build_turn_text(sender_name, sender_id, message, text, attachment_kind, attachment_path,
                    is_known_command=False, live_context="", in_flight=""):
    extras = []

    hints = []
    if _mentions_bot(message):
        hints.append("explicitly @mentioned the bot")
    if _is_reply_to_bot(message):
        hints.append("a reply to one of the bot's own messages")
    if is_known_command:
        hints.append("a recognized slash command")
    if hints:
        extras.append("Addressing signal (a hint, not a requirement): " + ", ".join(hints) + ".")

    if attachment_path:
        extras.append("Attached file, already resolved to a durable local path: " + render_attachment(attachment_kind, attachment_path))

    provenance = describe_message_provenance(message)
    if provenance:
        extras.append(
            "Where this message came from and what else it carries (treat a forward as "
            "someone else's words, not the sender's own):\n" + provenance
        )

    if live_context:
        extras.append("Live state as of right now:\n" + live_context)

    if in_flight:
        extras.append(
            "Other chats with a turn currently in flight (informational only, doesn't block "
            "or require coordinating with them):\n" + in_flight
        )

    body = f"Message from {sender_name} (id {sender_id}):\n{text or '(no text)'}"
    return body + ("\n\n" + "\n\n".join(extras) if extras else "")


def combine_turns(turn_texts):
    if len(turn_texts) == 1:
        return turn_texts[0]
    preamble = (
        f"[{len(turn_texts)} messages arrived together and are delivered as one turn - nobody has "
        f"typed for {MESSAGE_BATCH_QUIET_SECONDS}s since the last one. Read the whole run before "
        f"reacting to any part of it: it is usually a single thought split across several sends, or "
        f"the people in this chat working something out between themselves. Reply once, to whatever "
        f"it all adds up to - not once per message. If a later message corrects or cancels an "
        f"earlier one, the later one wins, and the earlier one needs no separate answer.]\n\n"
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


# --- reaction updates ----------------------------------------------------------

def reaction_list_emoji(reaction_list):
    parts = []
    for reaction in reaction_list or []:
        reaction_type = reaction.get("type")
        if reaction_type == "emoji":
            parts.append(reaction.get("emoji", ""))
        elif reaction_type == "custom_emoji":
            parts.append(f"custom:{reaction.get('custom_emoji_id')}")
        elif reaction_type == "paid":
            parts.append("⭐paid")
    return parts


def handle_message_reaction(reaction):
    """Someone added, removed or changed an emoji reaction on an existing
    message. Telegram only delivers these for a group/supergroup if the bot
    is an administrator there; a private chat has no such restriction.

    Persisted into the same transcript messages go into, so a reader of the
    chat history sees that a message was answered with a thumbs-up rather
    than seeing no answer at all.
    """
    chat_id = (reaction.get("chat") or {}).get("id")
    message_id = reaction.get("message_id")
    user = reaction.get("user") or {}
    actor_chat = reaction.get("actor_chat") or {}
    reactor_id = user.get("id")
    reactor_name = (
        user.get("first_name") or user.get("username")
        or actor_chat.get("title") or (str(reactor_id) if reactor_id is not None else None)
    )

    old_emoji = reaction_list_emoji(reaction.get("old_reaction"))
    new_emoji = reaction_list_emoji(reaction.get("new_reaction"))
    log(f"message_reaction: chat {chat_id} message {message_id} from {reactor_name!r} "
        f"({reactor_id}): {old_emoji} -> {new_emoji}")

    common.append_reaction(
        chat_id, message_id, emoji=",".join(new_emoji) or None,
        reactor_name=reactor_name, reactor_id=reactor_id,
    )


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
        # `/sleep`: keep reading and batching, deliver nothing until the
        # window ends. See `start_sleeping`.
        self.sleep_until = 0.0
        self.slept_note = ""

    def sleeping(self):
        return time.time() < self.sleep_until

    async def queue(self, message_id, turn_text, sender_name, sender_id):
        if not self.inbox:
            self.batch_started_at = time.time()
        self.inbox.append((message_id, turn_text, sender_name, sender_id))
        if len(self.inbox) >= MESSAGE_BATCH_MAX_MESSAGES and not self.sleeping():
            log(f"chat {self.chat_id}: batch hit {MESSAGE_BATCH_MAX_MESSAGES} messages, delivering without waiting out the quiet period")
            self._cancel_timer()
            self.flush()
            return
        self._restart_timer()

    def start_sleeping(self, seconds):
        """Stay out of the conversation for `seconds`, then deliver
        everything that arrived meanwhile as one late turn. Returns the line
        to say back to the chat."""
        if seconds <= 0:
            was_asleep = self.sleeping()
            self.sleep_until = 0.0
            # The note stays: messages really did arrive while it was
            # asleep, and they are still about to be delivered late.
            reply = "Awake." if was_asleep else "Was already awake."
        else:
            self.sleep_until = time.time() + seconds
            self.slept_note = (
                f"[You were asked to stay out of this conversation for a while (/sleep {seconds}), "
                f"and that window has now ended. Everything below arrived during it - you are seeing "
                f"it late, on purpose. They may well have settled it between themselves. If so, say "
                f"nothing, or say something short. Do not reopen a question they already answered.]\n\n"
            )
            reply = f"Staying out of it for {seconds}s. I'll keep reading and come back to it after."
        if self.inbox:
            self._restart_timer()
        return reply

    def drop_queued(self):
        """Throw away whatever is batched but not yet delivered. Returns how
        many messages were dropped."""
        dropped = len(self.inbox)
        self.inbox = []
        self.batch_started_at = 0.0
        self._cancel_timer()
        return dropped

    def _cancel_timer(self):
        if self.timer_task is not None and not self.timer_task.done():
            self.timer_task.cancel()
        self.timer_task = None

    def _restart_timer(self):
        self._cancel_timer()
        self.timer_task = asyncio.create_task(self._flush_when_quiet())

    async def _flush_when_quiet(self):
        while self.sleeping():
            await asyncio.sleep(max(0.05, self.sleep_until - time.time()))
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
        note, self.slept_note = self.slept_note, ""
        task = asyncio.create_task(self._dispatch_batch(batch, note))
        # Hold a reference until it finishes; asyncio only weakly
        # references running tasks and will otherwise collect one mid-turn.
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    async def _dispatch_batch(self, batch, note=""):
        message_ids = [mid for mid, _, _, _ in batch]
        text = note + combine_turns([t for _, t, _, _ in batch])
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
        self._turn_started_at = {}
        self._last_live_context = {}

    def builtin_commands(self):
        commands = [
            {"command": "help", "description": "List available commands and what each does"},
            {"command": "sleep", "description": (
                f"Stay out of it for N seconds (default {SLEEP_DEFAULT_SECONDS}). Messages keep being "
                f"read and are answered once, when the window ends. /sleep 0 wakes it now")},
        ]
        if tts_enabled():
            commands.append({"command": "voice", "description": (
                "With text after it: speak that text back as a voice message directly, no agent "
                "involved")})
        return commands

    def _api(self):
        # Every send primitive is handed over as a coroutine function: the
        # underlying calls are blocking `requests` calls, and running one
        # inline on the event loop would stall the poll loop and every
        # other chat for its full duration.
        return ListenerAPI(
            send_message=lambda chat_id, text, reply_to_message_id=None, disable_notification=False:
                asyncio.to_thread(send_stream_text, chat_id, text, self.api_base,
                                  reply_to_message_id, disable_notification),
            send_typing=lambda chat_id: asyncio.to_thread(send_typing_action, chat_id, self.api_base),
            set_reaction=lambda chat_id, message_id, emoji: asyncio.to_thread(
                set_message_reaction, chat_id, message_id, emoji, self.api_base),
            send_document=lambda chat_id, path, caption=None, reply_to_message_id=None:
                asyncio.to_thread(send_document, chat_id, path, self.api_base, caption, reply_to_message_id),
            edit_message=lambda chat_id, message_id, text: asyncio.to_thread(
                edit_message, chat_id, message_id, text, self.api_base),
            delete_message=lambda chat_id, message_id: asyncio.to_thread(
                delete_message, chat_id, message_id, self.api_base),
            send_voice=lambda chat_id, text, reply_to_message_id=None: asyncio.to_thread(
                send_voice_message, chat_id, text, self.api_base, reply_to_message_id),
            report_failure=lambda chat_id, what, exc=None: asyncio.to_thread(
                report_failure, chat_id, what, self.api_base, exc),
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

    def render_in_flight_chats(self, current_chat_id):
        """Which other chats are mid-turn right now. A plugin serving several
        people at once is otherwise answering each as if it were the only
        conversation happening."""
        now = time.time()
        lines = [
            f"Chat {chat_id} has a turn in flight, started {int(now - started)}s ago."
            for chat_id, started in sorted(self._turn_started_at.items())
            if chat_id != current_chat_id
        ]
        return "\n".join(lines)

    async def _live_context(self, chat_id):
        hook = getattr(self.plugin, "live_context", None)
        if hook is None:
            return ""
        now = time.time()
        if now - self._last_live_context.get(chat_id, 0.0) < LIVE_CONTEXT_REFRESH_INTERVAL_SECONDS:
            return ""
        self._last_live_context[chat_id] = now
        try:
            return await hook(chat_id) or ""
        except Exception:
            log(f"chat {chat_id}: plugin.live_context failed, continuing without it:\n{traceback.format_exc()}")
            return ""

    async def _typing_loop(self, chat_id, stop_event):
        """Hold the typing indicator up for as long as the turn runs.
        Telegram expires it after about five seconds, so one send at the
        start of a two-minute turn shows nothing for most of it."""
        while not stop_event.is_set():
            await asyncio.to_thread(send_typing_action, chat_id, self.api_base)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=TYPING_ACTION_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def _yield_to_in_flight_turn(self, chat_id):
        """A turn is already running for this chat and a new batch is ready.

        Wait out the grace period; if the running turn is still going after
        it, ask the plugin to interrupt it so the new message is read
        straight away instead of landing in the middle of work it may well
        have made pointless. Returns a note to prepend to the new turn.
        """
        deadline = time.time() + TURN_YIELD_GRACE_SECONDS
        while self._turn_lock(chat_id).locked() and time.time() < deadline:
            await asyncio.sleep(0.1)
        if not self._turn_lock(chat_id).locked():
            return ""

        interrupt = getattr(self.plugin, "interrupt_turn", None)
        if interrupt is None:
            return ""

        log(f"chat {chat_id}: turn still running after {TURN_YIELD_GRACE_SECONDS}s, asking the plugin to interrupt it")
        try:
            note = await interrupt(chat_id, self._api()) or ""
        except Exception as exc:
            await asyncio.to_thread(
                report_failure, chat_id,
                "I couldn't interrupt the turn that was still running, so this message lands in the middle of it.",
                self.api_base, exc)
            return ""
        return (note + "\n") if note else ""

    async def _dispatch_turn(self, chat_id, message_ids, text, sender_name, sender_id):
        note = ""
        if self._turn_lock(chat_id).locked():
            note = await self._yield_to_in_flight_turn(chat_id)

        # One turn at a time per chat. A plugin holds per-chat state (an
        # agent session, a subprocess) that concurrent turns would corrupt,
        # and a turn can outlast the next batch's quiet period.
        async with self._turn_lock(chat_id):
            self._turn_started_at[chat_id] = time.time()
            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(self._typing_loop(chat_id, stop_typing))
            turn = Turn(chat_id=chat_id, message_ids=message_ids, text=note + text,
                        sender_name=sender_name, sender_id=sender_id)
            try:
                await self.plugin.handle_turn(turn, self._api())
            except Exception:
                # The thumbs-down below is itself visible in the chat, and a
                # plugin that knows what broke reports it through
                # api.report_failure - so this doesn't also speak up and say
                # the same thing twice in worse words.
                log(f"chat {chat_id}: plugin.handle_turn failed:\n{traceback.format_exc()}")
                reaction = REACTION_FAILED
            else:
                reaction = REACTION_DONE
            finally:
                stop_typing.set()
                await typing_task
                self._turn_started_at.pop(chat_id, None)
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

    def _is_known_command(self, text):
        if not text:
            return False
        tokens = text.split()
        if not tokens or not tokens[0].startswith("/"):
            return False
        command = tokens[0][1:].split("@", 1)[0].lower()
        known = {c["command"] for c in self.builtin_commands() + self.plugin.describe_commands()}
        return command in known

    async def _handle_builtin_command(self, chat_id, message_id, command, argument):
        """Returns True when this was one of the listener's own commands."""
        if command == "help":
            await asyncio.to_thread(set_message_reaction, chat_id, message_id, REACTION_RECEIVED, self.api_base)
            lines = [f"/{c['command']} - {c['description']}"
                     for c in self.builtin_commands() + self.plugin.describe_commands()]
            await asyncio.to_thread(send_stream_text, chat_id, "\n".join(lines), self.api_base)
            return True

        if command == "sleep":
            try:
                seconds = int(float(argument)) if argument else SLEEP_DEFAULT_SECONDS
            except ValueError:
                await asyncio.to_thread(
                    send_message, chat_id,
                    f"I don't follow: {argument!r}. Use /sleep <seconds>, or /sleep 0 to wake me now.",
                    self.api_base)
                return True
            reply = self._batcher(chat_id).start_sleeping(seconds)
            await asyncio.to_thread(set_message_reaction, chat_id, message_id, REACTION_SLEEPING, self.api_base)
            await asyncio.to_thread(send_message, chat_id, reply, self.api_base)
            return True

        if command == "voice" and tts_enabled() and argument:
            # Speak the given text straight back. No agent turn: this is a
            # "say this out loud" button, not a question.
            await asyncio.to_thread(set_message_reaction, chat_id, message_id, REACTION_RECEIVED, self.api_base)
            await asyncio.to_thread(send_voice_message, chat_id, argument, self.api_base, message_id)
            await asyncio.to_thread(set_message_reaction, chat_id, message_id, REACTION_DONE, self.api_base)
            return True

        return False

    async def _transcribe_into_text(self, chat_id, message_id, kind, file_id, local_path, file_size, caption):
        """Replace a voice/audio/video message's empty body with what was
        actually said in it, and hand the transcript back to the chat as a
        file so the person can see what was heard."""
        label = MEDIA_LABELS[kind]
        if file_size > TRANSCRIBE_MAX_BYTES:
            note = f"[{label} - {file_size // (1024 * 1024)}MB, too large to transcribe; raw file saved at {local_path}]"
        else:
            try:
                transcript = await asyncio.to_thread(transcribe_media, local_path)
            except Exception as exc:
                log(f"chat {chat_id}: transcription failed for {local_path!r}: {exc!r}")
                transcript = None
            if transcript:
                note = f"[{label} transcript] {transcript}"
                transcript_path = await asyncio.to_thread(store_media_transcript, file_id, transcript)
                try:
                    await asyncio.to_thread(send_document, chat_id, transcript_path, self.api_base, "Transcript", message_id)
                except Exception as exc:
                    log(f"chat {chat_id}: sending the transcript file back failed: {exc!r}")
            else:
                note = f"[{label} - transcription failed, raw file saved at {local_path}]"
        caption = (caption or "").strip()
        return f"{caption}\n\n{note}" if caption else note

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
        is_known_command = self._is_known_command(stripped)

        if stripped.startswith("/"):
            command = stripped.split()[0][1:].split("@", 1)[0].lower()
            argument = stripped.split(maxsplit=1)[1].strip() if len(stripped.split(maxsplit=1)) > 1 else ""
            if await self._handle_builtin_command(chat_id, message_id, command, argument):
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
                if attachment_kind in TRANSCRIBABLE_KINDS and transcription_enabled():
                    text = await self._transcribe_into_text(
                        chat_id, message_id, attachment_kind, file_id, attachment_path,
                        file_size=(message.get(attachment_kind) or {}).get("file_size") or 0,
                        caption=text,
                    )
        except Exception as exc:
            await asyncio.to_thread(
                report_failure, chat_id, "Your attachment came in, but I couldn't unpack it.",
                self.api_base, exc)

        if not text and not attachment_path:
            if not describe_message_provenance(message):
                log(f"chat {chat_id}: message with no text/caption/attachment and nothing else describable - skipping")
                return
            text = "[No text and no downloadable attachment - see the block below for what this message actually was]"

        common.append_transcript(
            "incoming", chat_id, text=text or None, file_path=attachment_path or None,
            sender_name=sender_name, sender_id=sender_id, message_id=message_id)

        batcher = self._batcher(chat_id)
        await asyncio.to_thread(
            set_message_reaction, chat_id, message_id,
            REACTION_SLEEPING if batcher.sleeping() else REACTION_RECEIVED, self.api_base)

        turn_text = build_turn_text(
            sender_name, sender_id, message, text, attachment_kind, attachment_path,
            is_known_command=is_known_command,
            live_context=await self._live_context(chat_id),
            in_flight=self.render_in_flight_chats(chat_id),
        )
        log(f"ALLOWED: queued for chat {chat_id} - sender={sender_name} ({sender_id}) text={text!r} attachment={attachment_kind or 'none'}")
        await batcher.queue(message_id, turn_text, sender_name, sender_id)

    async def handle_update(self, update):
        message_reaction = update.get("message_reaction")
        if message_reaction:
            sender_id = (message_reaction.get("user") or {}).get("id")
            chat_id = (message_reaction.get("chat") or {}).get("id")
            allowed, reason = self._allowed(sender_id, chat_id)
            if not allowed:
                log(f"DROPPED message_reaction: {reason}")
                return
            await asyncio.to_thread(handle_message_reaction, message_reaction)
            return

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
        # Start the download before queuing for the chat lock, so the file
        # is on disk by the time the quiet period is out.
        await asyncio.to_thread(eager_resolve_attachment, message, self.api_base)

        async with self._chat_lock(chat_id):
            await self.handle_message(message)

    async def _handle_update_safe(self, update):
        try:
            await self.handle_update(update)
        except Exception as exc:
            message = update.get("message") or update.get("edited_message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            allowed, _ = self._allowed((message.get("from") or {}).get("id"), chat_id)
            # Only ever speak into a chat that already passed the gate -
            # an error message to an unauthorized sender is both a reply
            # they should not get and a signal the bot is listening.
            if chat_id is not None and allowed:
                await asyncio.to_thread(
                    report_failure, chat_id, "I couldn't process your message.", self.api_base, exc)
            else:
                log(f"update {update['update_id']}: unhandled error while processing:\n{traceback.format_exc()}")

    async def run(self):
        log("telegram listener starting")
        await asyncio.to_thread(
            register_bot_commands, self.api_base,
            self.builtin_commands() + self.plugin.describe_commands())
        await asyncio.to_thread(resolve_bot_identity, self.api_base)

        on_start = getattr(self.plugin, "on_start", None)
        if on_start is not None:
            await on_start(self._api())

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
