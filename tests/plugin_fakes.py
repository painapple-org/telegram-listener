"""Fakes for the example `claude_agent` plugin's tests only.

Importing this needs the optional `claude-agent-plugin` extra installed.
Keep it out of `tests/fakes.py`, which the core listener's tests import and
which must stay runnable on core dependencies alone.
"""

import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock


def fake_assistant_message(*blocks):
    return AssistantMessage(content=list(blocks), model="fake-model")


def fake_text_message(text):
    return fake_assistant_message(TextBlock(text=text))


def fake_tool_use_message(name):
    return fake_assistant_message(ToolUseBlock(id="tool-1", name=name, input={}))


def fake_result(is_error=False):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
        num_turns=1, session_id="fake-session", total_cost_usd=0.0,
    )


class FakeClaudeSDKClient:
    """Stands in for claude_agent_sdk.ClaudeSDKClient in plugin tests.

    Shaped like the real thing in the one way the plugin depends on:
    `receive_response` is a long-lived stream that does not end when the
    current turn does, so the plugin's reader task keeps running across
    turns. Each `query` enqueues this client's canned reply blocks followed
    by a ResultMessage, which is what actually ends a turn.
    """

    instances = []

    def __init__(self, options=None):
        self.options = options
        self.connected = False
        self.queries = []
        self.disconnected = False
        self.interrupted = False
        # What to emit per query: AssistantMessage blocks, then a result.
        self.reply_messages = [fake_text_message("(fake reply)")]
        self.result_is_error = False
        self._queue = asyncio.Queue()
        FakeClaudeSDKClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, text):
        self.queries.append(text)
        for message in self.reply_messages:
            await self._queue.put(message)
        await self._queue.put(fake_result(is_error=self.result_is_error))

    async def interrupt(self):
        self.interrupted = True
        await self._queue.put(fake_result())

    async def receive_response(self):
        while True:
            yield await self._queue.get()
