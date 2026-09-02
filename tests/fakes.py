"""Fakes shared across the test suite - no live network, no live Claude calls.

Nothing here imports `claude_agent_sdk`: that is the optional
`claude-agent-plugin` extra, needed only by the example plugin, and the core
listener's own tests have to run without it (see tests/plugin_fakes.py for
the Claude-specific fakes).
"""


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code
        self.text = ""

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
