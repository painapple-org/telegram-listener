import asyncio
import json
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


class TestReceiptReaction:
    def test_a_queued_message_is_acknowledged_immediately(self, monkeypatch):
        """The quiet period can run for several seconds before anything is
        dispatched, so a message with no acknowledgement at all looks lost."""
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 60)
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_message(make_message("hi", message_id=1)))

        reactions = [c for c in fake.calls if c[1] == "setMessageReaction"]
        assert reactions[0][2]["json"]["reaction"][0]["emoji"] == listener.REACTION_RECEIVED

    def test_an_emoji_telegram_will_not_accept_is_never_sent(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        listener.set_message_reaction(100, 1, "\U0001F9C0", "http://fake")
        assert [c for c in fake.calls if c[1] == "setMessageReaction"] == []


class TestSleep:
    def test_sleep_holds_the_batch_until_the_window_ends(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message("/sleep 1", message_id=1))
            await lst.handle_message(make_message("while you were out", message_id=2))
            await asyncio.sleep(0.1)
            assert plugin.turns == []
            # Wake it early rather than waiting the window out for real.
            lst._batcher(100).start_sleeping(0)
            await asyncio.sleep(0.2)

        asyncio.run(go())
        assert len(plugin.turns) == 1
        assert "while you were out" in plugin.turns[0].text
        assert "stay out of this conversation" in plugin.turns[0].text

    def test_sleep_zero_wakes_it_up(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})
        batcher = lst._batcher(100)
        batcher.start_sleeping(60)
        assert batcher.sleeping() is True
        assert batcher.start_sleeping(0) == "Awake."
        assert batcher.sleeping() is False

    def test_a_message_arriving_during_sleep_is_marked_as_held(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})
        lst._batcher(100).start_sleeping(60)

        asyncio.run(lst.handle_message(make_message("hello", message_id=2)))

        reactions = [c[2]["json"]["reaction"][0]["emoji"] for c in fake.calls if c[1] == "setMessageReaction"]
        assert listener.REACTION_SLEEPING in reactions

    def test_a_nonsense_duration_is_answered_not_guessed_at(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_message(make_message("/sleep ages", message_id=1)))

        body = [c for c in fake.calls if c[1] == "sendMessage"][-1][2]["data"]["text"]
        assert "/sleep <seconds>" in body
        assert lst._batcher(100).sleeping() is False


class TestNonFileContent:
    def test_a_poll_is_described_rather_than_dropped(self):
        message = make_message("", poll={
            "question": "lunch?", "type": "regular", "is_anonymous": True,
            "options": [{"text": "yes"}, {"text": "no"}],
        })
        described = listener.describe_message_provenance(message)
        assert "lunch?" in described and "yes | no" in described

    def test_a_location_carries_a_link(self):
        described = listener.describe_message_provenance(
            make_message("", location={"latitude": 52.1, "longitude": 4.3}))
        assert "52.1,4.3" in described

    def test_album_membership_is_explained(self):
        described = listener.describe_message_provenance(make_message("", media_group_id="77"))
        assert "album" in described

    def test_a_chat_event_is_described(self):
        described = listener.describe_message_provenance(
            make_message("", new_chat_title="A new name"))
        assert "A new name" in described

    def test_an_unhandled_bot_api_field_is_reported_not_swallowed(self):
        described = listener.describe_message_provenance(
            make_message("hi", some_future_telegram_field={"x": 1}))
        assert "some_future_telegram_field" in described
        assert listener.RAW_UPDATES_LOG in described

    def test_a_handled_field_is_not_reported_as_unknown(self):
        assert listener.describe_unknown_fields(make_message("hi")) == []


class TestProvenanceDetail:
    def test_an_external_reply_says_it_cannot_be_read(self):
        described = listener.describe_message_provenance(make_message("hi", external_reply={
            "chat": {"id": 7, "title": "Somewhere else"}, "message_id": 3}))
        assert "DIFFERENT chat" in described and "Somewhere else" in described

    def test_an_inline_bot_composition_is_flagged(self):
        described = listener.describe_message_provenance(
            make_message("hi", via_bot={"username": "gif"}))
        assert "@gif" in described

    def test_a_replied_to_media_message_names_its_kind(self):
        described = listener.describe_message_provenance(make_message("hi", reply_to_message={
            "message_id": 2, "from": {"id": 42, "first_name": "Xander"}, "voice": {"file_id": "f"}}))
        assert "(no text - a voice)" in described


class TestMessageReactions:
    def test_an_inbound_reaction_is_recorded_in_the_transcript(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        import telegram_listener.common as common
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_update({"update_id": 1, "message_reaction": {
            "chat": {"id": 100}, "message_id": 5, "user": {"id": 42, "first_name": "Xander"},
            "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "\U0001F44D"}],
        }}))

        entries = common.read_transcript()
        assert entries[-1]["direction"] == "reaction"
        assert entries[-1]["emoji"] == "\U0001F44D"

    def test_a_stranger_reacting_is_not_recorded(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        import telegram_listener.common as common
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_update({"update_id": 1, "message_reaction": {
            "chat": {"id": 100}, "message_id": 5, "user": {"id": 999},
            "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "\U0001F44D"}],
        }}))

        assert common.read_transcript() == []

    def test_reaction_updates_are_requested_explicitly(self, monkeypatch):
        fake = FakeRequests({"getUpdates": {"ok": True, "result": []}})
        monkeypatch.setattr(listener, "requests", fake)
        listener.get_updates(None, "http://fake")
        allowed = json.loads(fake.calls[0][2]["params"]["allowed_updates"])
        # Telegram omits these entirely unless they are named.
        assert "message_reaction" in allowed


class TestTranscription:
    def test_a_voice_message_arrives_as_what_was_said(self, monkeypatch, tmp_path):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "STT_ENDPOINT", "http://stt/v1/audio/transcriptions")
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        monkeypatch.setattr(listener, "download_and_store_file",
                            lambda file_id, filename, api_base: str(tmp_path / "voice.ogg"))
        monkeypatch.setattr(listener, "transcribe_media", lambda path: "can you look at the deploy")
        monkeypatch.setattr(listener, "send_document",
                            lambda *args, **kwargs: None)
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message(
                "", message_id=1, voice={"file_id": "f1", "file_size": 100}))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert "can you look at the deploy" in plugin.turns[0].text

    def test_an_oversized_file_is_handed_over_as_a_path_instead(self, monkeypatch, tmp_path):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        monkeypatch.setattr(listener, "STT_ENDPOINT", "http://stt/v1/audio/transcriptions")
        monkeypatch.setattr(listener, "TRANSCRIBE_MAX_BYTES", 10)
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        text = asyncio.run(lst._transcribe_into_text(
            100, 1, "video", "f1", "/tmp/big.mp4", file_size=50 * 1024 * 1024, caption=""))
        assert "too large to transcribe" in text
        assert "/tmp/big.mp4" in text

    def test_transcription_stays_off_until_an_endpoint_is_configured(self, monkeypatch):
        monkeypatch.setattr(listener, "STT_ENDPOINT", "")
        assert listener.transcription_enabled() is False


class TestVoiceReplies:
    def test_voice_is_only_offered_when_tts_is_configured(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})
        monkeypatch.setattr(listener, "TTS_ENDPOINT", "")
        assert "voice" not in {c["command"] for c in lst.builtin_commands()}
        monkeypatch.setattr(listener, "TTS_ENDPOINT", "http://tts/v1/audio/speech")
        assert "voice" in {c["command"] for c in lst.builtin_commands()}

    def test_voice_with_text_speaks_it_back_without_a_turn(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "TTS_ENDPOINT", "http://tts/v1/audio/speech")
        monkeypatch.setattr(listener, "synthesize_speech", lambda text: b"ogg")
        plugin = RecordingPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_message(make_message("/voice hello there", message_id=1)))

        assert plugin.turns == []
        assert [c for c in fake.calls if c[1] == "sendVoice"]

    def test_a_failed_synthesis_falls_back_to_text_rather_than_silence(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "synthesize_speech", lambda text: None)

        spoke = listener.send_voice_message(100, "hallo", "http://fake")

        assert spoke is False
        body = [c for c in fake.calls if c[1] == "sendMessage"][-1][2]["data"]["text"]
        assert body.startswith("hallo")
        assert listener.VOICE_UNAVAILABLE_NOTE.strip() in body


class TestReplyThreading:
    def test_only_the_first_chunk_carries_the_reply(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MAX_TELEGRAM_TEXT", 5)

        listener.send_stream_text(100, "abcdefghij", "http://fake", reply_to_message_id=7)

        sends = [c[2]["data"] for c in fake.calls if c[1] == "sendMessage"]
        assert "reply_parameters" in sends[0]
        assert "reply_parameters" not in sends[1]

    def test_a_reply_survives_its_target_being_deleted(self):
        parameters = json.loads(listener._reply_parameters(7))
        assert parameters["allow_sending_without_reply"] is True


class TestFailureReporting:
    def test_a_failure_is_said_in_the_chat_not_only_logged(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)

        listener.report_failure(100, "I couldn't read that.", "http://fake", ValueError("nope"))

        body = [c for c in fake.calls if c[1] == "sendMessage"][-1][2]["data"]["text"]
        assert "I couldn't read that." in body
        assert "ValueError: nope" in body

    def test_an_unreadable_attachment_is_reported_to_the_sender(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)

        def boom(*args, **kwargs):
            raise RuntimeError("container gone")

        monkeypatch.setattr(listener, "download_and_store_file", boom)
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message(
                "look at this", message_id=1, document={"file_id": "f1", "file_name": "a.pdf"}))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        bodies = [c[2]["data"]["text"] for c in fake.calls if c[1] == "sendMessage"]
        assert any("couldn't unpack it" in body for body in bodies)


class TestEditMessage:
    def test_an_unchanged_edit_is_not_a_failure(self, monkeypatch):
        class NotModified:
            status_code = 400
            text = ""

            def json(self):
                return {"ok": False, "description": "Bad Request: message is not modified"}

        class Requests:
            def post(self, url, **kwargs):
                return NotModified()

        monkeypatch.setattr(listener, "requests", Requests())
        assert listener.edit_message(100, 5, "same", "http://fake") is True


class TestLiveContext:
    def test_the_plugin_hook_is_folded_in_and_not_re_asked_every_message(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        calls = []

        class ContextPlugin(RecordingPlugin):
            async def live_context(self, chat_id):
                calls.append(chat_id)
                return "branch: main"

        plugin = ContextPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message("one", message_id=1))
            await asyncio.sleep(0.05)
            await lst.handle_message(make_message("two", message_id=2))
            await asyncio.sleep(0.05)

        asyncio.run(go())
        assert calls == [100]
        assert "branch: main" in plugin.turns[0].text
        assert "branch: main" not in plugin.turns[1].text

    def test_a_plugin_without_the_hook_is_fine(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})
        assert asyncio.run(lst._live_context(100)) == ""


class TestInFlightChats:
    def test_another_chats_running_turn_is_mentioned(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})
        lst._turn_started_at[200] = listener.time.time()

        assert "Chat 200" in lst.render_in_flight_chats(100)
        assert lst.render_in_flight_chats(200) == ""


class TestInterruptHook:
    def test_an_overrunning_turn_is_interrupted_for_a_new_message(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        monkeypatch.setattr(listener, "TURN_YIELD_GRACE_SECONDS", 0)

        release = asyncio.Event()

        class SlowPlugin(RecordingPlugin):
            def __init__(self):
                super().__init__()
                self.interrupted = []

            async def handle_turn(self, turn, api):
                self.turns.append(turn)
                if len(self.turns) == 1:
                    await release.wait()

            async def interrupt_turn(self, chat_id, api):
                self.interrupted.append(chat_id)
                release.set()
                return "[the previous turn was interrupted]"

        plugin = SlowPlugin()
        lst = listener.Listener(plugin=plugin, api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message("slow one", message_id=1))
            await asyncio.sleep(0.05)
            await lst.handle_message(make_message("never mind", message_id=2))
            await asyncio.sleep(0.3)

        asyncio.run(go())
        assert plugin.interrupted == [100]
        assert plugin.turns[1].text.startswith("[the previous turn was interrupted]")

    def test_without_the_hook_the_new_turn_simply_waits(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        monkeypatch.setattr(listener, "TURN_YIELD_GRACE_SECONDS", 0)
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})
        assert asyncio.run(lst._yield_to_in_flight_turn(100)) == ""


class TestTypingKeepalive:
    def test_typing_is_held_up_for_the_whole_turn(self, monkeypatch):
        """Telegram expires the typing indicator after a few seconds, so one
        send at the start of a long turn shows nothing for most of it."""
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        monkeypatch.setattr(listener, "TYPING_ACTION_INTERVAL", 0.02)

        class SlowPlugin(RecordingPlugin):
            async def handle_turn(self, turn, api):
                self.turns.append(turn)
                await asyncio.sleep(0.1)

        lst = listener.Listener(plugin=SlowPlugin(), api_base="http://fake", allowed_user_ids={42})

        async def go():
            await lst.handle_message(make_message("hi", message_id=1))
            await asyncio.sleep(0.3)

        asyncio.run(go())
        assert len([c for c in fake.calls if c[1] == "sendChatAction"]) > 1


class TestOnStart:
    def test_a_plugin_without_on_start_still_runs(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})
        assert getattr(lst.plugin, "on_start", None) is None

    def test_on_start_is_handed_the_listener_api(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        seen = []

        class StartingPlugin(RecordingPlugin):
            async def on_start(self, api):
                seen.append(api)

        lst = listener.Listener(plugin=StartingPlugin(), api_base="http://fake", allowed_user_ids={42})
        asyncio.run(lst.plugin.on_start(lst._api()))
        assert seen and callable(seen[0].send_message)


class TestEagerAttachmentResolution:
    def test_the_download_starts_before_the_quiet_period_is_out(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 60)
        downloads = []
        monkeypatch.setattr(listener, "download_and_store_file",
                            lambda file_id, filename, api_base: downloads.append(file_id) or "/tmp/f")
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_update(make_update(make_message(
            "see attached", message_id=1, document={"file_id": "f1", "file_name": "a.pdf"}))))

        assert downloads[0] == "f1"

    def test_a_failed_eager_download_is_left_for_the_real_one_to_report(self, monkeypatch):
        fake = FakeRequests()
        monkeypatch.setattr(listener, "requests", fake)

        def boom(*args, **kwargs):
            raise RuntimeError("not yet")

        monkeypatch.setattr(listener, "download_and_store_file", boom)
        # Deliberately does not raise: handle_message downloads again and
        # reports properly if it still cannot.
        listener.eager_resolve_attachment(
            make_message("x", document={"file_id": "f1", "file_name": "a.pdf"}), "http://fake")


class TestTranscriptLinkage:
    def test_an_incoming_message_records_its_message_id(self, monkeypatch):
        monkeypatch.setattr(listener, "requests", FakeRequests())
        monkeypatch.setattr(listener, "MESSAGE_BATCH_QUIET_SECONDS", 0)
        import telegram_listener.common as common
        lst = listener.Listener(plugin=RecordingPlugin(), api_base="http://fake", allowed_user_ids={42})

        asyncio.run(lst.handle_message(make_message("hello", message_id=77)))

        incoming = [e for e in common.read_transcript() if e["direction"] == "incoming"]
        assert incoming[-1]["message_id"] == 77
