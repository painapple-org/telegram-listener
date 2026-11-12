"""Example DispatchPlugin: a persistent claude-agent-sdk session per chat.

This is the concrete proof that the listener's plugin interface is real:
one resumable Claude session per Telegram chat, replies streamed back as
they arrive, implemented purely as a `DispatchPlugin` and wired up through
`DISPATCH_PLUGIN=plugins.claude_agent:build_plugin` rather than being
hard-coded into the listener.

It is also meant to be subclassed. A deployment running a Claude session
of its own needs none of this session machinery rewritten - what it needs
is its own turn text, its own live context, its own commands and its own
agent configuration. So `ClaudeAgentPlugin` keeps everything specific to
*running an agent* here and leaves exactly those seams open:
`configure_options` (anything about this deployment's agent -
working directory, extra readable directories, which CLI binary),
`fresh_session_note`, plus the `DispatchPlugin` hooks themselves.
Copying this file to change four things is the drift this package exists
to prevent.

What it does on top of bare session persistence - and why each of those is
here rather than in the listener:

- A long-lived reader task per chat, so a reply streams out block by block
  instead of arriving all at once at the end of a turn.
- An activity message that is edited in place while tools run and deleted
  as soon as real text arrives, so a multi-minute turn isn't silence.
- Compaction, both when the context grows past a cap and after a chat has
  been idle for a while, plus a daily session rotation. All three are facts
  about running an agent with a bounded context window; none is a fact
  about Telegram.
- Meta-commands (`/compact`, `/clear`, `/context`, `/cost`, `/recap`,
  `/model`) passed through to the agent CLI, with its answer sent back.
- `/cancel`, and the listener's `interrupt_turn` hook, both backed by the
  SDK client's own interrupt.

`claude-agent-sdk` is this module's own dependency (the
`claude-agent-plugin` extra in pyproject.toml) - the listener core never
imports it and works fine with a plugin that doesn't either.
"""

import asyncio
import json
import os
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolUseBlock,
)

from telegram_listener import common

DEFAULT_SYSTEM_PROMPT = (
    "You are responding to messages in a Telegram chat via a long-polling "
    "listener. Reply plainly and directly - there is no special formatting "
    "support beyond plain text."
)

# Compact once the context passes this, rather than letting a turn fail on
# a full window.
COMPACT_CONTEXT_TOKENS = int(os.environ.get("CLAUDE_AGENT_COMPACT_CONTEXT_TOKENS", 200_000))

# Compaction and the meta-commands both run a whole agent turn; neither is
# instant and neither should hang forever.
COMPACT_WAIT_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_AGENT_COMPACT_WAIT_TIMEOUT_SECONDS", 180))

# After this much silence a chat's context is compacted and the session
# disconnected, so an idle chat isn't holding a live subprocess open.
IDLE_COMPACT_SECONDS = int(os.environ.get("CLAUDE_AGENT_IDLE_COMPACT_SECONDS", 3 * 60 * 60))
IDLE_CHECK_INTERVAL_SECONDS = int(os.environ.get("CLAUDE_AGENT_IDLE_CHECK_INTERVAL_SECONDS", 15 * 60))

# A session is retired at the first message of a new local day, provided
# the chat has also been quiet for a while - rotating mid-conversation
# because midnight passed would drop context someone is still using.
SESSION_ROTATE_TZ = ZoneInfo(os.environ.get("CLAUDE_AGENT_TZ", "UTC"))
SESSION_ROTATE_MIN_IDLE_SECONDS = int(os.environ.get("CLAUDE_AGENT_SESSION_ROTATE_MIN_IDLE_SECONDS", 30 * 60))

# How many retired session ids are remembered per chat, so an older
# conversation can still be resumed by hand if anyone needs to.
SESSION_HISTORY_LENGTH = 30

# Editing the activity message on every single tool call hits Telegram's
# rate limit and flickers for the reader.
ACTIVITY_EDIT_MIN_INTERVAL_SECONDS = 2.0

MAX_ACTIVITY_TEXT = 3800

# Commands handed straight to the agent CLI, whose answer is sent back as
# the whole reply. These are the CLI's own meta-commands, not this
# plugin's - it only knows which ones to forward.
META_PASSTHROUGH_COMMANDS = {"compact", "clear", "context", "cost", "recap", "model"}

COMMANDS = [
    {"command": "compact", "description": "Summarize this chat's context now, instead of waiting for the idle timeout"},
    {"command": "clear", "description": "Wipe this chat's context entirely - the next message starts completely fresh"},
    {"command": "context", "description": "Show how much of this chat's context window is used, and on what"},
    {"command": "cost", "description": "Show token usage/cost for this chat's session"},
    {"command": "recap", "description": "One-line summary of what this chat's session has been doing"},
    {"command": "model", "description": "Show or switch (e.g. /model opus) the model this chat uses"},
    {"command": "cancel", "description": "Abort a turn that's currently in flight for this chat"},
]

REACTION_RECEIVED = "\U0001F440"
REACTION_DONE = "\U0001F44D"


def _sessions_file():
    return os.path.join(common.resolve_data_dir(), "telegram-listener", "claude_agent_sessions.json")


def _load_sessions():
    path = _sessions_file()
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        # Deliberately not caught: an unreadable sessions file silently
        # orphans every chat's history, which looks exactly like the agent
        # having forgotten everything for no reason.
        return json.load(f)


def _save_sessions(sessions):
    path = _sessions_file()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(sessions, f)
    os.replace(tmp_path, path)


def session_record(sessions, chat_id):
    """Normalize whatever is stored for `chat_id` into the current shape."""
    raw = sessions.get(str(chat_id))
    if isinstance(raw, dict):
        return {"session_id": raw.get("session_id"), "started": raw.get("started"),
                "previous": raw.get("previous") or []}
    return {"session_id": None, "started": None, "previous": []}


def local_today():
    return datetime.now(SESSION_ROTATE_TZ).date().isoformat()


def context_tokens(usage):
    """Everything currently occupying the context window, cached or not."""
    if not usage:
        return None
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total or None


def fresh_session_note(today, last_day):
    return (
        f"[This is a new session, started fresh for {today}. It is NOT your first - you have been "
        f"working with these people for a while, and the last session ran on {last_day}. You do not "
        f"have any of that conversation in context, and you must not pretend otherwise or "
        f"reconstruct it from guesswork. If this message refers to something you don't recognise - "
        f"a decision, a nickname, a branch, 'the thing we discussed', a half-finished piece of work "
        f"- look it up in whatever record you have rather than guessing, and say plainly when you "
        f"are going from the record rather than from memory.]\n\n"
    )


class ChatSession:
    """One resumable ClaudeSDKClient, plus everything that has to be true
    for exactly one chat: its reader task, its activity message, its
    in-flight turn."""

    def __init__(self, chat_id, plugin):
        self.chat_id = chat_id
        self.plugin = plugin
        self.client = None
        self.session_id = None
        self.connect_lock = asyncio.Lock()
        self.reader_task = None
        self.api = None

        self.pending = []
        self.turn_reply_to = None
        self.turn_spoke = False
        self.turn_done_event = None
        self.turn_error = ""

        self.last_message_time = 0.0
        self.last_context_tokens = 0
        self.compacted_since_activity = False

        self.activity_message_id = None
        self.activity_tools = {}
        self.activity_last_edit = 0.0
        self.activity_last_text = ""
        self.active_tasks = {}

        self.fresh_session_note = ""
        self.interrupting = False
        self.tasks_killed_by_interrupt = []

        self.compact_done_event = None
        self.meta_command_event = None
        self.meta_command_reply_text = ""
        self.meta_command_was_error = False

    # --- connection ------------------------------------------------------

    async def ensure_connected(self, api):
        self.api = api
        async with self.connect_lock:
            if self.client is not None:
                return
            sessions = _load_sessions()
            record = session_record(sessions, self.chat_id)
            existing_id = record["session_id"]

            options = ClaudeAgentOptions(
                permission_mode="bypassPermissions",
                include_partial_messages=True,
                system_prompt={"type": "preset", "preset": "claude_code",
                               "append": self.plugin.system_prompt},
            )
            if self.plugin.model:
                options.model = self.plugin.model
            self.plugin.configure_options(options)
            if existing_id:
                options.resume = existing_id
                self.session_id = existing_id
                api.log(f"chat {self.chat_id}: resuming existing session {existing_id}")
            else:
                self.session_id = str(uuid.uuid4())
                options.session_id = self.session_id
                sessions[str(self.chat_id)] = {
                    "session_id": self.session_id,
                    "started": local_today(),
                    "previous": record["previous"],
                }
                _save_sessions(sessions)
                api.log(f"chat {self.chat_id}: starting new session {self.session_id}")

            self.client = self.plugin.client_factory(options=options)
            await self.client.connect()
            self.reader_task = asyncio.create_task(self._read_loop())

    async def disconnect(self):
        if self.client is None:
            return
        client, self.client = self.client, None
        if self.reader_task is not None:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass
            self.reader_task = None
        try:
            await client.disconnect()
        except Exception:
            self._log(f"chat {self.chat_id}: disconnect failed:\n{traceback.format_exc()}")

    def _log(self, message):
        if self.api is not None:
            self.api.log(message)

    # --- reading the agent's output --------------------------------------

    async def _read_loop(self):
        try:
            while True:
                async for msg in self.client.receive_response():
                    await self._consume(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log(f"chat {self.chat_id}: reader loop ended:\n{traceback.format_exc()}")
            self.client = None
            self.reader_task = None
            # Release whoever is waiting on this turn, as failed. Without
            # this the turn waits on an event nothing will ever set, and the
            # chat's turn lock is held forever.
            self.pending = []
            self.turn_error = "the agent session's reader loop died mid-turn"
            if self.turn_done_event is not None:
                self.turn_done_event.set()

    async def _consume(self, msg):
        is_top_level_assistant = isinstance(msg, AssistantMessage) and getattr(msg, "parent_tool_use_id", None) is None

        if is_top_level_assistant:
            tokens = context_tokens(getattr(msg, "usage", None))
            if tokens is not None:
                self.last_context_tokens = tokens
            await self._stream_assistant_message(msg)

        if isinstance(msg, TaskStartedMessage):
            self.active_tasks[msg.task_id] = msg.description
        elif isinstance(msg, (TaskNotificationMessage, TaskUpdatedMessage)):
            if getattr(msg, "status", None) in TERMINAL_TASK_STATUSES:
                description = self.active_tasks.pop(msg.task_id, None)
                if self.interrupting and description:
                    self.tasks_killed_by_interrupt.append(description)

        if self.meta_command_event is not None and isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    self.meta_command_reply_text += block.text

        if isinstance(msg, ResultMessage):
            await self._clear_activity()
            await self._on_result(msg)

    def _streaming_suppressed(self):
        """While a meta-command or a compaction is running, the agent's
        output is that operation's own bookkeeping, not a reply to anyone."""
        return self.meta_command_event is not None or self.compact_done_event is not None

    async def _stream_assistant_message(self, msg):
        if self._streaming_suppressed():
            return
        for block in msg.content:
            if isinstance(block, TextBlock):
                text = block.text.strip()
                if not text:
                    continue
                await self._clear_activity()
                reply_to = None if self.turn_spoke else self.turn_reply_to
                await self.api.send_message(self.chat_id, text, reply_to_message_id=reply_to)
                self.turn_spoke = True
            elif isinstance(block, ToolUseBlock):
                self.activity_tools[block.name] = self.activity_tools.get(block.name, 0) + 1
                await self._update_activity()

    async def _on_result(self, msg):
        """A turn, a compaction or a meta-command just finished. Which of
        the three it was is whichever one is currently waiting.

        Per-message reactions are deliberately not set here: the listener
        sets them from whether `handle_turn` returned or raised, and two
        owners of the same reaction means the later write silently wins.
        """
        pending, self.pending = self.pending, []
        if pending:
            if msg.is_error:
                self.turn_error = "the agent turn ended in an error"
            if self.turn_done_event is not None:
                self.turn_done_event.set()
        elif self.compact_done_event is not None:
            self.compact_done_event.set()
        elif self.meta_command_event is not None:
            self.meta_command_was_error = msg.is_error
            self.meta_command_event.set()

    # --- the activity message --------------------------------------------

    def render_activity(self):
        parts = [f"{name}×{count}" if count > 1 else name
                 for name, count in self.activity_tools.items()]
        line = "\U0001F527 working - " + ", ".join(parts) if parts else "\U0001F527 working"
        if self.active_tasks:
            line += "\n⤷ " + "; ".join(list(self.active_tasks.values())[:3])
        return line[:MAX_ACTIVITY_TEXT]

    async def _update_activity(self):
        if self._streaming_suppressed():
            return
        text = self.render_activity()
        now = time.time()
        if self.activity_message_id is not None:
            if text == self.activity_last_text or now - self.activity_last_edit < ACTIVITY_EDIT_MIN_INTERVAL_SECONDS:
                return
            self.activity_last_edit = now
            self.activity_last_text = text
            await self.api.edit_message(self.chat_id, self.activity_message_id, text)
            return
        self.activity_last_edit = now
        self.activity_last_text = text
        # Silent: a progress note should not buzz anyone's phone.
        self.activity_message_id = await self.api.send_message(
            self.chat_id, text, disable_notification=True)

    async def _clear_activity(self):
        message_id, self.activity_message_id = self.activity_message_id, None
        self.activity_tools = {}
        self.activity_last_text = ""
        if message_id is not None:
            await self.api.delete_message(self.chat_id, message_id)

    # --- turns -----------------------------------------------------------

    async def handle_turn(self, turn, api):
        await self.maybe_rotate_session(api)
        await self.ensure_connected(api)
        await self.maybe_compact_if_oversized(api)
        await self.ensure_connected(api)

        self.last_message_time = time.time()
        self.compacted_since_activity = False
        self.pending.extend(turn.message_ids)
        self.turn_reply_to = turn.message_ids[-1] if turn.message_ids else None
        self.turn_spoke = False
        note, self.fresh_session_note = self.fresh_session_note, ""

        self.turn_error = ""
        self.turn_done_event = asyncio.Event()
        await self.client.query(note + turn.text)
        # The listener reacts to the turn's messages once handle_turn
        # returns, and holds the chat's turn lock until then - so returning
        # before the agent has actually finished would both mark the turn
        # done while it is still talking and let the next turn in on top of
        # it.
        await self.turn_done_event.wait()
        self.turn_done_event = None
        if self.turn_error:
            raise RuntimeError(self.turn_error)

    async def interrupt_turn(self, api):
        """Abort whatever is running so a newly arrived message is read now.
        Returns the note explaining that to the agent."""
        if self.client is None or not self.pending:
            return ""

        self.interrupting = True
        self.tasks_killed_by_interrupt = []
        try:
            await self.client.interrupt()
        except Exception as exc:
            self.interrupting = False
            await api.report_failure(
                self.chat_id,
                "I couldn't interrupt the turn that was still running, so this message lands in the middle of it.",
                exc)
            return ""
        finally:
            killed, self.tasks_killed_by_interrupt = self.tasks_killed_by_interrupt, []
            self.interrupting = False
            self.pending = []
            self.turn_error = "the turn was interrupted to read a newly arrived message"
            if self.turn_done_event is not None:
                self.turn_done_event.set()

        note = (
            "[The turn that was still running when this message arrived was interrupted "
            "so this could be read straight away, instead of being spliced into the "
            "middle of it.]\n"
        )
        if killed:
            note += (
                "[That interrupt also killed these dispatched agents before they finished, so "
                "their work did NOT land: " + "; ".join(killed) + ". Re-dispatch whatever is "
                "still wanted once you've read the message below - and if the message doesn't "
                "make them unnecessary, say plainly that you're re-dispatching them.]\n"
            )
        return note

    async def cancel_pending(self, message_id, api):
        # Messages that arrived while the cancelled work ran are part of
        # what is being cancelled - answering them afterwards replies to a
        # conversation the person has already walked away from.
        dropped = api.drop_queued(self.chat_id)
        if self.client is None or not self.pending:
            await api.send_message(
                self.chat_id,
                f"Nothing in flight. Dropped {dropped} message(s) that hadn't been delivered yet."
                if dropped else "Nothing in flight to cancel.")
            return
        try:
            await self.client.interrupt()
            await api.send_message(self.chat_id, "Cancelled.")
        except Exception as exc:
            await api.report_failure(self.chat_id, "Cancelling that turn failed.", exc)
        finally:
            self.pending = []
            self.turn_error = "the turn was cancelled on request"
            if self.turn_done_event is not None:
                self.turn_done_event.set()

    # --- session lifecycle ------------------------------------------------

    async def maybe_rotate_session(self, api):
        """Retire yesterday's session at the first message of a new local
        day, as long as the chat has been quiet long enough that nobody is
        mid-thought."""
        sessions = _load_sessions()
        record = session_record(sessions, self.chat_id)
        today = local_today()
        if not record["session_id"] or record["started"] == today:
            return
        if time.time() - self.last_message_time < SESSION_ROTATE_MIN_IDLE_SECONDS:
            return

        retiring = record["session_id"]
        previous = ([{"session_id": retiring, "date": record["started"]}] + record["previous"])[:SESSION_HISTORY_LENGTH]
        sessions[str(self.chat_id)] = {"session_id": None, "started": None, "previous": previous}
        _save_sessions(sessions)
        api.log(f"chat {self.chat_id}: new day ({today}), retiring session {retiring} "
                f"(last used {record['started']}) and starting fresh")
        self.fresh_session_note = self.plugin.fresh_session_note(today, record["started"] or "an earlier day")
        await self.disconnect()

    async def _run_compaction(self, api, on_failure=None):
        self.compact_done_event = asyncio.Event()
        try:
            await self.client.query("/compact")
            await asyncio.wait_for(self.compact_done_event.wait(), timeout=COMPACT_WAIT_TIMEOUT_SECONDS)
        except Exception as exc:
            if on_failure is not None:
                await api.report_failure(self.chat_id, on_failure, exc)
            else:
                api.log(f"chat {self.chat_id}: /compact wait failed: {exc!r}")
        finally:
            self.compact_done_event = None
        self.last_context_tokens = 0

    async def maybe_compact_if_oversized(self, api):
        if self.client is None or self.pending:
            return
        if self.last_context_tokens < COMPACT_CONTEXT_TOKENS:
            return
        api.log(f"chat {self.chat_id}: context at {self.last_context_tokens} tokens "
                f"(cap {COMPACT_CONTEXT_TOKENS}), compacting before the next turn")
        await self._run_compaction(
            api, on_failure="Compacting the context failed - I'm carrying on with a full one, so I may be paying less attention.")

    async def maybe_compact_if_idle(self, api):
        if self.client is None or self.compacted_since_activity:
            return
        if time.time() - self.last_message_time < IDLE_COMPACT_SECONDS:
            return
        api.log(f"chat {self.chat_id}: idle {IDLE_COMPACT_SECONDS}s+, compacting then disconnecting")
        await self._run_compaction(api)
        self.compacted_since_activity = True
        await self.disconnect()

    # --- meta-commands ----------------------------------------------------

    async def run_meta_command(self, message_id, command, argument, api):
        await self.ensure_connected(api)
        self.last_message_time = time.time()
        self.compacted_since_activity = False
        await api.set_reaction(self.chat_id, message_id, REACTION_RECEIVED)

        command_text = f"/{command} {argument}".strip()
        self.meta_command_reply_text = ""
        self.meta_command_was_error = False
        self.meta_command_event = asyncio.Event()
        reply_text = ""
        try:
            await self.client.query(command_text)
            await asyncio.wait_for(self.meta_command_event.wait(), timeout=COMPACT_WAIT_TIMEOUT_SECONDS)
            reply_text = self.meta_command_reply_text
            if self.meta_command_was_error and not reply_text:
                reply_text = f"(/{command} returned an error - see the listener log)"
        except Exception:
            api.log(f"chat {self.chat_id}: run_meta_command({command_text!r}) failed:\n{traceback.format_exc()}")
            reply_text = reply_text or f"(/{command} failed - see the listener log)"
        finally:
            self.meta_command_event = None

        await api.set_reaction(self.chat_id, message_id, REACTION_DONE)
        await api.send_message(self.chat_id, reply_text or f"(/{command} said nothing)")

        if command in ("compact", "clear"):
            self.last_context_tokens = 0
        if command == "clear":
            sessions = _load_sessions()
            sessions.pop(str(self.chat_id), None)
            _save_sessions(sessions)
            await self.disconnect()
        if command == "compact":
            self.compacted_since_activity = True
            await self.disconnect()


class ClaudeAgentPlugin:
    """One `ChatSession` per Telegram chat_id."""

    def __init__(self, client_factory=None, system_prompt=DEFAULT_SYSTEM_PROMPT, model=None):
        # `client_factory` is injectable for tests - production code leaves
        # it as the real ClaudeSDKClient constructor.
        self.client_factory = client_factory or ClaudeSDKClient
        self.system_prompt = system_prompt
        self.model = model
        self._sessions = {}
        self._watchdog_task = None

    def describe_commands(self):
        return list(COMMANDS)

    def configure_options(self, options):
        """Last word on the `ClaudeAgentOptions` a chat's session connects
        with. Everything about *this deployment's* agent rather than about
        running an agent at all - a working directory, extra readable
        directories, a specific CLI binary - belongs in an override of
        this, not in a copy of this file."""

    def fresh_session_note(self, today, last_day):
        """What a session rotated onto a new day is told about the one it
        replaces. Overridable because where a deployment keeps the record
        it should look things up in is the deployment's own business."""
        return fresh_session_note(today, last_day)

    def session(self, chat_id):
        session = self._sessions.get(chat_id)
        if session is None:
            session = ChatSession(chat_id, self)
            self._sessions[chat_id] = session
        return session

    async def on_start(self, api):
        self._watchdog_task = asyncio.create_task(self._idle_watchdog(api))

    async def _idle_watchdog(self, api):
        while True:
            await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
            for session in list(self._sessions.values()):
                try:
                    await session.maybe_compact_if_idle(api)
                except Exception:
                    api.log(f"idle watchdog: chat {session.chat_id} failed:\n{traceback.format_exc()}")

    async def handle_turn(self, turn, api):
        await self.session(turn.chat_id).handle_turn(turn, api)

    async def interrupt_turn(self, chat_id, api):
        return await self.session(chat_id).interrupt_turn(api)

    async def handle_command(self, chat_id, message_id, command, argument, api):
        if command in META_PASSTHROUGH_COMMANDS:
            await self.session(chat_id).run_meta_command(message_id, command, argument, api)
            return True
        if command == "cancel":
            await self.session(chat_id).cancel_pending(message_id, api)
            return True
        return False


def build_plugin():
    return ClaudeAgentPlugin(
        system_prompt=os.environ.get("CLAUDE_AGENT_SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT,
        model=os.environ.get("CLAUDE_AGENT_MODEL"),
    )
