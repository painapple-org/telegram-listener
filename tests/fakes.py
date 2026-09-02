"""Fakes shared across the test suite - no live network, no live Claude calls."""

from claude_agent_sdk import AssistantMessage, TextBlock


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


class FakeRequests:
    """Records every call made through it and returns pre-seeded responses.

    `responses` maps a bare endpoint name (e.g. "sendMessage") to either a
    single dict-shaped Telegram API response, or a list consumed in order.
    """

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []

    def _respond(self, method, url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((method, endpoint, kwargs))
        seeded = self.responses.get(endpoint, {"ok": True, "result": {}})
        if isinstance(seeded, list):
            seeded = seeded.pop(0) if seeded else {"ok": True, "result": {}}
        return FakeResponse(seeded)

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)


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
