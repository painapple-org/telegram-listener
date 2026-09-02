"""Fakes for the example `claude_agent` plugin's tests only.

Importing this needs the optional `claude-agent-plugin` extra installed.
Keep it out of `tests/fakes.py`, which the core listener's tests import and
which must stay runnable on core dependencies alone.
"""

from claude_agent_sdk import AssistantMessage, TextBlock


def fake_assistant_message(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="fake-model")


class FakeClaudeSDKClient:
    """Stands in for claude_agent_sdk.ClaudeSDKClient in plugin tests."""

    instances = []

    def __init__(self, options=None):
        self.options = options
        self.connected = False
        self.queries = []
        self.disconnected = False
        self.reply_texts = ["(fake reply)"]
        FakeClaudeSDKClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        for text in self.reply_texts:
            yield fake_assistant_message(text)
