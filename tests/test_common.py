import json
import os

import telegram_listener.common as common


class TestRedact:
    def test_token_in_a_url_is_removed(self):
        message = "ConnectionError: failed to reach http://127.0.0.1:8081/bot123456789:AAExampleToken-abc/getUpdates"
        redacted = common.redact(message)
        assert "AAExampleToken-abc" not in redacted
        assert "123456789" not in redacted
        assert "/bot<redacted>/getUpdates" in redacted

    def test_bare_token_is_removed(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAExampleToken-abc")
        assert "AAExampleToken-abc" not in common.redact("token was 123456789:AAExampleToken-abc somewhere")

    def test_text_without_a_token_is_untouched(self):
        assert common.redact("chat 100: delivering 2 messages") == "chat 100: delivering 2 messages"


class TestResolveDataDir:
    def test_explicit_env_var_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TELEGRAM_LISTENER_DATA_DIR", str(tmp_path))
        assert common.resolve_data_dir() == str(tmp_path)

    def test_falls_back_to_cwd_data_when_unset(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_LISTENER_DATA_DIR", raising=False)
        monkeypatch.setattr(os.path, "isdir", lambda path: False)
        assert common.resolve_data_dir() == os.path.join(os.getcwd(), "data")


class TestTranscript:
    def test_a_message_id_links_an_entry_back_to_its_telegram_message(self):
        common.append_transcript("incoming", 100, text="hi", message_id=7)
        assert common.read_transcript()[-1]["message_id"] == 7

    def test_a_reaction_is_recorded_against_the_message_it_targets(self):
        common.append_reaction(100, 7, emoji="\U0001F44D", reactor_name="Xander", reactor_id=42)
        entry = common.read_transcript()[-1]
        assert entry["direction"] == "reaction"
        assert (entry["message_id"], entry["emoji"], entry["reactor_id"]) == (7, "\U0001F44D", 42)

    def test_a_cleared_reaction_carries_no_emoji(self):
        common.append_reaction(100, 7, emoji=None)
        assert "emoji" not in common.read_transcript()[-1]


class TestReactionEmoji:
    def test_an_accepted_emoji_round_trips(self):
        assert common.canonical_reaction_emoji("\U0001F44D") == "\U0001F44D"

    def test_a_variation_selector_does_not_make_it_unrecognizable(self):
        """Clients send ❤️, the Bot API wants ❤ - the same reaction."""
        assert common.canonical_reaction_emoji("❤️") == "❤"

    def test_an_emoji_telegram_does_not_accept_is_rejected(self):
        assert common.canonical_reaction_emoji("\U0001F9C0") is None


class TestOutOfBandMarking:
    def test_the_listeners_own_sends_are_marked(self):
        common.append_transcript("outgoing", 100, text="mine", from_listener=True)
        assert common.read_transcript()[-1]["from_listener"] is True

    def test_an_unmarked_send_is_not_claimed_by_the_listener(self):
        """Absence of the mark is what makes a send out-of-band, so a
        caller that forgets to identify itself errs toward being seen."""
        common.append_transcript("outgoing", 100, text="theirs", sent_by="cron")
        entry = common.read_transcript()[-1]
        assert "from_listener" not in entry
        assert entry["sent_by"] == "cron"


class TestTranscriptCursor:
    def test_only_entries_after_the_cursor_come_back(self):
        common.append_transcript("outgoing", 100, text="first")
        fresh, index = common.read_transcript_since(0)
        assert [e["text"] for e in fresh] == ["first"]

        common.append_transcript("outgoing", 100, text="second")
        fresh, _ = common.read_transcript_since(index)
        assert [e["text"] for e in fresh] == ["second"]

    def test_the_returned_index_counts_every_chat_not_the_filtered_ones(self):
        """Counting only the matching entries would let a second chat's
        traffic silently shift this chat's cursor past unread messages."""
        common.append_transcript("outgoing", 100, text="mine")
        common.append_transcript("outgoing", 999, text="someone else's")
        fresh, index = common.read_transcript_since(0, chat_id=100)
        assert [e["text"] for e in fresh] == ["mine"]
        assert index == 2


class TestTranscriptObserver:
    def test_observer_sees_every_entry_with_the_offset_it_landed_at(self, isolated_data_dir):
        import telegram_listener.common as common

        seen = []
        common.set_transcript_observer(lambda direction, entry, offset: seen.append((direction, entry, offset)))
        try:
            common.append_transcript("incoming", 100, text="hello", sender_name="Alice")
            common.append_transcript("outgoing", 100, text="hi", from_listener=True)
            common.append_reaction(100, 7, emoji="\U0001F44D", reactor_name="Alice")
        finally:
            common.set_transcript_observer(None)

        assert [direction for direction, _entry, _offset in seen] == ["incoming", "outgoing", "reaction"]
        assert seen[0][2] == 0
        with open(common.TRANSCRIPT_FILE, "rb") as f:
            raw = f.read()
        # Each recorded offset is where that entry's own line starts.
        for _direction, entry, offset in seen:
            assert json.loads(raw[offset:].split(b"\n", 1)[0]) == entry

    def test_a_failing_observer_does_not_lose_the_entry(self, isolated_data_dir):
        import telegram_listener.common as common

        def boom(direction, entry, offset):
            raise RuntimeError("downstream is down")

        common.set_transcript_observer(boom)
        try:
            common.append_transcript("incoming", 100, text="still lands")
        finally:
            common.set_transcript_observer(None)

        assert [entry["text"] for entry in common.read_transcript(limit=None)] == ["still lands"]
