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


class FakeListenerAPI:
    """A `ListenerAPI` whose every primitive just records the call.

    Built here rather than per-test so that adding a primitive to the
    interface breaks in one place instead of in every test that fakes it.
    """

    def __init__(self):
        self.sent = []
        self.typing = []
        self.reactions = []
        self.documents = []
        self.edits = []
        self.deletes = []
        self.voice = []
        self.failures = []
        self.logs = []
        self.dropped = []
        self.queued_count = 0
        self._next_message_id = 1000

    def as_listener_api(self):
        from telegram_listener.plugin import ListenerAPI

        return ListenerAPI(
            send_message=self._send_message,
            send_typing=self._send_typing,
            set_reaction=self._set_reaction,
            send_document=self._send_document,
            edit_message=self._edit_message,
            delete_message=self._delete_message,
            send_voice=self._send_voice,
            report_failure=self._report_failure,
            drop_queued=self._drop_queued,
            log=self.logs.append,
        )

    async def _send_message(self, chat_id, text, reply_to_message_id=None, disable_notification=False):
        self._next_message_id += 1
        self.sent.append((chat_id, text, reply_to_message_id, disable_notification))
        return self._next_message_id

    async def _send_typing(self, chat_id):
        self.typing.append(chat_id)

    async def _set_reaction(self, chat_id, message_id, emoji):
        self.reactions.append((chat_id, message_id, emoji))

    async def _send_document(self, chat_id, path, caption=None, reply_to_message_id=None):
        self.documents.append((chat_id, path, caption, reply_to_message_id))

    async def _edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True

    async def _delete_message(self, chat_id, message_id):
        self.deletes.append((chat_id, message_id))

    async def _send_voice(self, chat_id, text, reply_to_message_id=None):
        self.voice.append((chat_id, text, reply_to_message_id))
        return True

    async def _report_failure(self, chat_id, what, exc=None):
        self.failures.append((chat_id, what, exc))

    def _drop_queued(self, chat_id):
        self.dropped.append(chat_id)
        count, self.queued_count = self.queued_count, 0
        return count

    def texts(self):
        return [text for _chat_id, text, _reply_to, _silent in self.sent]
