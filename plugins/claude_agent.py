"""Example DispatchPlugin: spawn a persistent claude-agent-sdk session per chat.

This is the concrete proof that the listener's plugin interface is real: it
is spoor's own current behavior (one resumable Claude session per Telegram
chat, replies streamed back as they arrive) reimplemented purely as a
`DispatchPlugin`, wired up through `DISPATCH_PLUGIN=plugins.claude_agent:build_plugin`
rather than being hard-coded into the listener.

`claude-agent-sdk` is this module's own dependency (the `claude-agent-plugin`
extra in pyproject.toml) - the listener core never imports it and works
fine with a plugin that doesn't either.

This intentionally does not reproduce every feature of spoor's own
`ChatSessionManager` (activity-bubble editing, daily session rotation,
context-size-triggered auto-compaction, `/sleep`/`/cancel` turn
interruption, voice replies) - those are real spoor product behaviors, not
requirements of the plugin interface itself. What's here - session
persistence across process restarts, streamed text replies, and a couple
of meta-commands - is enough to prove a real plugin can drive a real
multi-turn agent session through this interface.
"""

import json
import os
import uuid
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from telegram_listener import common
from telegram_listener.plugin import ListenerAPI, Turn

DEFAULT_SYSTEM_PROMPT = (
    "You are responding to messages in a Telegram chat via a long-polling "
    "listener. Reply plainly and directly - there is no special formatting "
    "support beyond plain text."
)


def _sessions_file():
    return os.path.join(common.resolve_data_dir(), "telegram-listener", "claude_agent_sessions.json")


def _load_sessions():
    path = _sessions_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sessions(sessions):
    path = _sessions_file()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(sessions, f)
    os.replace(tmp_path, path)


class ClaudeAgentPlugin:
    """One resumable ClaudeSDKClient session per Telegram chat_id."""

    COMMANDS = [
        {"command": "clear", "description": "Wipe this chat's session and start completely fresh"},
        {"command": "compact", "description": "Summarize this chat's context now"},
    ]

    def __init__(self, client_factory=None, system_prompt=DEFAULT_SYSTEM_PROMPT, model=None):
        # `client_factory` is injectable for tests - production code leaves
        # it as the real ClaudeSDKClient constructor.
        self._client_factory = client_factory or ClaudeSDKClient
        self._system_prompt = system_prompt
        self._model = model
        self._clients = {}

    def describe_commands(self):
        return list(self.COMMANDS)

    async def _client_for(self, chat_id):
        client = self._clients.get(chat_id)
        if client is not None:
            return client

        sessions = _load_sessions()
        existing_id = sessions.get(str(chat_id))

        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            system_prompt={"type": "preset", "preset": "claude_code", "append": self._system_prompt},
        )
        if self._model:
            options.model = self._model
        if existing_id:
            options.resume = existing_id
            session_id = existing_id
        else:
            session_id = str(uuid.uuid4())
            options.session_id = session_id
            sessions[str(chat_id)] = session_id
            _save_sessions(sessions)

        client = self._client_factory(options=options)
        await client.connect()
        self._clients[chat_id] = client
        return client

    async def _disconnect(self, chat_id):
        client = self._clients.pop(chat_id, None)
        if client is not None:
            await client.disconnect()

    async def handle_turn(self, turn: Turn, api: ListenerAPI) -> None:
        api.send_typing(turn.chat_id)
        client = await self._client_for(turn.chat_id)
        await client.query(turn.text)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        api.send_message(turn.chat_id, block.text.strip())

    async def handle_command(self, chat_id, message_id, command, argument, api: ListenerAPI) -> bool:
        if command == "clear":
            await self._disconnect(chat_id)
            sessions = _load_sessions()
            sessions.pop(str(chat_id), None)
            _save_sessions(sessions)
            api.set_reaction(chat_id, message_id, "\U0001F44D")
            api.send_message(chat_id, "Cleared - next message starts a brand-new session.")
            return True
        if command == "compact":
            client = await self._client_for(chat_id)
            await client.query("/compact")
            async for message in client.receive_response():
                pass
            api.set_reaction(chat_id, message_id, "\U0001F44D")
            api.send_message(chat_id, "Compacted.")
            return True
        return False


def build_plugin():
    return ClaudeAgentPlugin(model=os.environ.get("CLAUDE_AGENT_MODEL"))
