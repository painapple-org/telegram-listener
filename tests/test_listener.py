import asyncio
import os

import pytest

import telegram_listener.listener as listener
from telegram_listener.plugin import ListenerAPI, Turn
from tests.fakes import FakeRequests


class RecordingPlugin:
    """A trivial DispatchPlugin that just records what it was handed."""

    def __init__(self):
        self.turns = []
        self.commands = []

    async def handle_turn(self, turn, api):
        self.turns.append(turn)
        await api.send_message(turn.chat_id, f"echo: {turn.text}")

    async def handle_command(self, chat_id, message_id, command, argument, api):
        self.commands.append((chat_id, message_id, command, argument))
        if command == "known":
            await api.send_message(chat_id, "handled")
            return True
        return False

    def describe_commands(self):
        return [{"command": "known", "description": "a plugin-owned command"}]


def make_message(text, message_id=1, chat_id=100, sender_id=42, sender_name="Xander", **extra):
    message = {
        "message_id": message_id,
        "date": 0,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": sender_id, "first_name": sender_name},
        "text": text,
    }
    message.update(extra)
    return message


def make_update(message, update_id=1, key="message"):
    return {"update_id": update_id, key: message}


class TestAllowlist:
    def test_disallowed_sender_is_dropped(self):
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={1})
        asyncio.run(lst.handle_message(make_message("hi", sender_id=999)))
        assert lst.plugin.turns == []

    def test_allowlisted_sender_in_unlisted_chat_is_dropped(self):
        lst = listener.Listener(
            plugin=RecordingPlugin(), api_base="http://fake",
            allowed_user_ids={42}, allowed_chat_ids={555},
        )
        asyncio.run(lst.handle_message(make_message("hi", sender_id=42, chat_id=100)))
        assert lst.plugin.turns == []

    def test_empty_chat_allowlist_permits_any_chat(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        lst = listener.Listener(
            plugin=RecordingPlugin(), api_base="http://fake",
            allowed_user_ids={42}, allowed_chat_ids=set(),
        )

        async def go():
            await lst.handle_message(make_message("hi", sender_id=42, chat_id=100))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert len(lst.plugin.turns) == 1

    def test_empty_user_allowlist_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
        with pytest.raises(RuntimeError, match="allowlist nobody"):
            listener._allowed_user_ids()


class TestRawUpdateLog:
    def test_disallowed_senders_content_is_never_written_to_disk(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_update(make_update(make_message("secret text", sender_id=999))))

        assert not os.path.exists(listener.RAW_UPDATES_LOG)

    def test_allowed_senders_update_is_recorded(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_update(make_update(make_message("hello", sender_id=42)))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        with open(listener.RAW_UPDATES_LOG) as f:
            assert "hello" in f.read()


class TestEditedMessages:
    def test_an_edit_does_not_rerun_the_turn(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_update(make_update(
                make_message("fixed typo", sender_id=42), key="edited_message"))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert plugin.turns == []


class TestSenderAttribution:
    def test_each_turn_reports_its_own_sender(self, monkeypatch):
        """A batcher lives for the whole process, so a group chat's second
        turn must not be attributed to whoever spoke first."""
        monkeypatch.setattr(listener, "requests", FakeRequests())
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42, 43})

        async def go():
            await lst.handle_message(make_message("from a", message_id=1, sender_id=42, sender_name="Xander"))
            await asyncio.sleep(0.05)
            await lst.handle_message(make_message("from b", message_id=2, sender_id=43, sender_name="David"))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert [(t.sender_name, t.sender_id) for t in plugin.turns] == [("Xander", 42), ("David", 43)]


class TestTurnSerialization:
    def test_turns_for_one_chat_never_overlap(self, monkeypatch):
        """A plugin holds per-chat state a concurrent second turn would
        corrupt, and a turn can outlast the next batch's quiet period."""
        monkeypatch.setattr(listener, "requests", FakeRequests())
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)

        class SlowPlugin(RecordingPlugin):
            def __init__(self):
                super().__init__()
                self.concurrent = 0
                self.max_concurrent = 0

            async def handle_turn(self, turn, api):
                self.concurrent += 1
                self.max_concurrent = max(self.max_concurrent, self.concurrent)
                await asyncio.sleep(0.05)
                self.concurrent -= 1
                self.turns.append(turn)

        plugin = SlowPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message("one", message_id=1))
            await asyncio.sleep(0.01)
            await lst.handle_message(make_message("two", message_id=2))
            await asyncio.sleep(0.3)

        asyncio.run(go())
        assert plugin.max_concurrent == 1
        assert len(plugin.turns) == 2

    def test_a_new_message_does_not_kill_a_running_turn(self, monkeypatch):
        """A later message restarts the quiet-period timer. That must not
        cancel a turn already being dispatched."""
        monkeypatch.setattr(listener, "requests", FakeRequests())
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)

        class SlowPlugin(RecordingPlugin):
            async def handle_turn(self, turn, api):
                await asyncio.sleep(0.1)
                self.turns.append(turn)

        plugin = SlowPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message("one", message_id=1))
            await asyncio.sleep(0.02)
            await lst.handle_message(make_message("two", message_id=2))
            await asyncio.sleep(0.5)

        asyncio.run(go())
        assert [t.message_ids for t in plugin.turns] == [[1], [2]]


class TestResponseChecking:
    def test_an_http_error_never_quotes_the_request_url(self):
        """`requests.raise_for_status` puts the token-bearing URL into its
        message, and that message reaches logs and plugin code."""
        from tests.fakes import FakeResponse

        resp = FakeResponse({}, status_code=401)
        with pytest.raises(RuntimeError) as caught:
            listener._check_response(resp, "getUpdates")
        assert "getUpdates" in str(caught.value)
        assert "bot" not in str(caught.value)


class TestTurnDispatch:
    def test_single_message_becomes_one_turn(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)

        async def go():
            await lst.handle_message(make_message("hello there", message_id=1))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert len(plugin.turns) == 1
        turn = plugin.turns[0]
        assert isinstance(turn, Turn)
        assert turn.chat_id == 100
        assert turn.message_ids == [1]
        assert "hello there" in turn.text
        assert any(call[1] == "sendMessage" for call in fake.calls)

    def test_burst_of_messages_collapses_into_one_turn(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0.05)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_MAX_WAIT_SECONDS", 5)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message("first", message_id=1))
            await lst.handle_message(make_message("second", message_id=2))
            await asyncio.sleep(0.2)

        asyncio.run(go())
        assert len(plugin.turns) == 1
        turn = plugin.turns[0]
        assert turn.message_ids == [1, 2]
        assert "first" in turn.text and "second" in turn.text
        assert "2 messages arrived together" in turn.text

    def test_max_messages_flushes_without_waiting(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 60)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_MAX_MESSAGES", 2)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message("a", message_id=1))
            await lst.handle_message(make_message("b", message_id=2))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert len(plugin.turns) == 1
        assert plugin.turns[0].message_ids == [1, 2]


class TestHelpCommand:
    def test_help_lists_builtin_and_plugin_commands(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_message(make_message("/help", message_id=1)))

        send_calls = [c for c in fake.calls if c[1] == "sendMessage"]
        assert send_calls
        body = send_calls[-1][2]["data"]["text"]
        assert "/help" in body
        assert "/known" in body


class TestPluginCommand:
    def test_recognized_plugin_command_short_circuits_turn_dispatch(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_message(make_message("/known extra arg", message_id=1)))

        assert plugin.commands == [(100, 1, "known", "extra arg")]
        assert plugin.turns == []

    def test_unrecognized_command_falls_through_to_turn_dispatch(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message("/notacommand", message_id=1))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert plugin.commands == [(100, 1, "notacommand", "")]
        assert len(plugin.turns) == 1


class TestProvenance:
    def test_reply_provenance_is_described(self):
        message = make_message(
            "yes", message_id=2,
            reply_to_message={"message_id": 1, "from": {"id": 1, "first_name": "Bot"}, "text": "question?"},
        )
        provenance = listener.describe_message_provenance(message)
        assert "reply to" in provenance
        assert "question?" in provenance

    def test_forward_provenance_is_described(self):
        message = make_message(
            "fyi", message_id=3,
            forward_origin={"type": "user", "sender_user": {"id": 7, "first_name": "Someone"}},
        )
        provenance = listener.describe_message_provenance(message)
        assert "FORWARD" in provenance
        assert "Someone" in provenance

    def test_no_provenance_is_empty_string(self):
        message = make_message("plain message", message_id=4)
        assert listener.describe_message_provenance(message) == ""


class TestCallbackQuery:
    def test_disallowed_user_is_rejected_without_recording(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={1})
        lst.handle_callback_query({
            "id": "cbq1", "from": {"id": 999}, "data": "yes",
            "message": {"chat": {"id": 100}, "message_id": 5},
        })
        answer_calls = [c for c in fake.calls if c[1] == "answerCallbackQuery"]
        assert len(answer_calls) == 1
        assert answer_calls[0][2]["json"]["text"] == "Not authorized."

    def test_allowed_user_records_confirmation_answer(self, monkeypatch, tmp_path):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        import telegram_listener.common as common
        common.create_pending_confirmation(100, 5, question="proceed?")

        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={1})
        lst.handle_callback_query({
            "id": "cbq2", "from": {"id": 1}, "data": "yes",
            "message": {"chat": {"id": 100}, "message_id": 5},
        })
        record = common.get_confirmation(100, 5)
        assert record["status"] == "answered"
        assert record["answer"] == "yes"


class TestStatePersistence:
    def test_state_round_trips(self, tmp_path):
        listener.save_state({"last_update_id": 42})
        assert listener.load_state() == {"last_update_id": 42}

    def test_missing_state_file_is_empty_dict(self):
        assert listener.load_state() == {}


class TestAttachmentExtraction:
    def test_photo_picks_largest_size(self):
        message = {"photo": [{"file_id": "small"}, {"file_id": "big"}]}
        file_id, kind, _ = listener.extract_attachment(message)
        assert file_id == "big"
        assert kind == "photo"

    def test_no_attachment_returns_none(self):
        assert listener.extract_attachment({"text": "hi"}) is None
