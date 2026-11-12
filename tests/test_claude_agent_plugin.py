import asyncio
import importlib

import pytest

from telegram_listener.plugin import Turn
from tests.fakes import FakeListenerAPI

# The example plugin - and only the example plugin - needs the optional
# `claude-agent-plugin` extra. Skip rather than error when it isn't
# installed, so `uv run pytest` on core dependencies alone still passes.
pytest.importorskip("claude_agent_sdk", reason="optional claude-agent-plugin extra not installed")

import plugins.claude_agent as claude_agent  # noqa: E402
from tests.plugin_fakes import (  # noqa: E402
    FakeClaudeSDKClient,
    fake_text_message,
    fake_tool_use_message,
)


@pytest.fixture
def plugin_module(tmp_path, monkeypatch):
    """A freshly reloaded plugin module whose session file lives in tmp_path."""
    monkeypatch.setenv("TELEGRAM_LISTENER_DATA_DIR", str(tmp_path))
    import telegram_listener.common as common

    importlib.reload(common)
    importlib.reload(claude_agent)
    FakeClaudeSDKClient.instances.clear()
    yield claude_agent


def make_turn(chat_id=1, message_ids=(10,), text="hello"):
    return Turn(chat_id=chat_id, message_ids=list(message_ids), text=text,
                sender_name="Xander", sender_id=42)


def run_turn(plugin, api, turn):
    async def main():
        await plugin.handle_turn(turn, api)
        await plugin.session(turn.chat_id).disconnect()

    asyncio.run(main())


class TestTurns:
    def test_handle_turn_streams_reply_through_api(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()
        run_turn(plugin, api.as_listener_api(), make_turn())

        assert api.texts() == ["(fake reply)"]
        assert FakeClaudeSDKClient.instances[0].queries == ["hello"]

    def test_the_first_reply_threads_onto_the_triggering_message(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()
        run_turn(plugin, api.as_listener_api(), make_turn(message_ids=(10, 11)))

        # The last message of the batch is the one being answered.
        assert api.sent[0][2] == 11

    def test_handle_turn_only_returns_once_the_agent_has_finished(self, plugin_module):
        """The listener holds the chat's turn lock until handle_turn returns,
        so returning early would let the next turn in on top of this one."""
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()
        run_turn(plugin, api.as_listener_api(), make_turn())

        session = plugin.session(1)
        assert session.pending == []
        assert session.turn_error == ""

    def test_an_error_result_makes_the_turn_raise(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()

        def failing_factory(options=None):
            client = FakeClaudeSDKClient(options=options)
            client.result_is_error = True
            return client

        plugin.client_factory = failing_factory
        with pytest.raises(RuntimeError):
            run_turn(plugin, api.as_listener_api(), make_turn())


class TestActivityMessage:
    def test_tool_use_posts_a_silent_activity_message_and_deletes_it_on_reply(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()

        def factory(options=None):
            client = FakeClaudeSDKClient(options=options)
            client.reply_messages = [fake_tool_use_message("Read"), fake_text_message("done")]
            return client

        plugin.client_factory = factory
        run_turn(plugin, api.as_listener_api(), make_turn())

        activity, reply = api.sent[0], api.sent[1]
        assert "Read" in activity[1]
        # Silent: a progress note must not buzz anyone's phone.
        assert activity[3] is True
        assert reply[1] == "done"
        # The activity message is removed once there is real text to read.
        assert len(api.deletes) == 1 and api.deletes[0][0] == 1

    def test_repeated_tool_calls_are_counted_not_listed(self, plugin_module):
        session = plugin_module.ChatSession(1, plugin_module.ClaudeAgentPlugin())
        session.activity_tools = {"Read": 3, "Bash": 1}
        rendered = session.render_activity()
        assert "Read×3" in rendered
        assert "Bash" in rendered and "Bash×" not in rendered


class TestSessionPersistence:
    def test_session_id_persists_across_plugin_instances(self, plugin_module):
        api = FakeListenerAPI().as_listener_api()

        plugin_a = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        run_turn(plugin_a, api, make_turn(chat_id=5, message_ids=(1,), text="first"))
        first_session_id = FakeClaudeSDKClient.instances[0].options.session_id

        # A brand-new plugin instance (simulating a process restart) resumes
        # the same session for the same chat, instead of starting fresh.
        plugin_b = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        run_turn(plugin_b, api, make_turn(chat_id=5, message_ids=(2,), text="second"))

        assert FakeClaudeSDKClient.instances[1].options.resume == first_session_id

    def test_a_new_local_day_retires_the_session_and_notes_it(self, plugin_module, monkeypatch):
        api = FakeListenerAPI().as_listener_api()
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        run_turn(plugin, api, make_turn(chat_id=7))

        monkeypatch.setattr(plugin_module, "local_today", lambda: "2999-01-01")
        monkeypatch.setattr(plugin_module, "SESSION_ROTATE_MIN_IDLE_SECONDS", 0)
        plugin.session(7).last_message_time = 0.0
        run_turn(plugin, api, make_turn(chat_id=7, text="next day"))

        retired = plugin_module._load_sessions()["7"]
        assert retired["previous"][0]["session_id"] == FakeClaudeSDKClient.instances[0].options.session_id
        # The agent is told it has lost the old context rather than left to
        # invent what it can't remember.
        assert "NOT your first" in FakeClaudeSDKClient.instances[1].queries[0]

    def test_rotation_waits_for_the_chat_to_go_quiet(self, plugin_module, monkeypatch):
        api = FakeListenerAPI().as_listener_api()
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        run_turn(plugin, api, make_turn(chat_id=8))

        monkeypatch.setattr(plugin_module, "local_today", lambda: "2999-01-01")
        # Mid-conversation: the day changed but nobody has stopped talking.
        plugin.session(8).last_message_time = plugin_module.time.time()
        run_turn(plugin, api, make_turn(chat_id=8, text="still talking"))

        assert plugin_module._load_sessions()["8"]["session_id"] is not None


class TestCommands:
    def test_describe_commands_lists_plugin_owned_commands(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        names = {c["command"] for c in plugin.describe_commands()}
        assert {"clear", "compact", "context", "cost", "recap", "model", "cancel"} <= names

    def test_unrecognized_command_returns_false(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI().as_listener_api()
        assert asyncio.run(plugin.handle_command(1, 1, "notmine", "", api)) is False

    def test_meta_command_is_passed_through_and_its_answer_sent_back(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()

        def factory(options=None):
            client = FakeClaudeSDKClient(options=options)
            client.reply_messages = [fake_text_message("context: 12%")]
            return client

        plugin.client_factory = factory

        async def main():
            handled = await plugin.handle_command(3, 4, "context", "", api.as_listener_api())
            await plugin.session(3).disconnect()
            return handled

        assert asyncio.run(main()) is True
        assert FakeClaudeSDKClient.instances[0].queries == ["/context"]
        assert api.texts() == ["context: 12%"]

    def test_clear_drops_the_stored_session(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()
        run_turn(plugin, api.as_listener_api(), make_turn(chat_id=9))
        assert "9" in plugin_module._load_sessions()

        asyncio.run(plugin.handle_command(9, 2, "clear", "", api.as_listener_api()))
        assert "9" not in plugin_module._load_sessions()

    def test_cancel_drops_messages_still_waiting_out_the_quiet_period(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()
        api.queued_count = 3
        asyncio.run(plugin.handle_command(1, 1, "cancel", "", api.as_listener_api()))
        assert api.dropped == [1]
        assert api.texts() == ["Nothing in flight. Dropped 3 message(s) that hadn't been delivered yet."]

    def test_cancel_with_nothing_running_says_so(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()
        asyncio.run(plugin.handle_command(1, 1, "cancel", "", api.as_listener_api()))
        assert api.texts() == ["Nothing in flight to cancel."]


class TestInterrupt:
    def test_interrupt_turn_ends_the_running_turn_and_returns_a_note(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI().as_listener_api()

        async def main():
            session = plugin.session(2)
            await session.ensure_connected(api)
            session.pending = [5]
            note = await plugin.interrupt_turn(2, api)
            await session.disconnect()
            return note, session

        note, session = asyncio.run(main())
        assert "interrupted" in note
        assert FakeClaudeSDKClient.instances[0].interrupted is True
        assert session.pending == []

    def test_interrupt_with_nothing_in_flight_is_a_no_op(self, plugin_module):
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI().as_listener_api()
        assert asyncio.run(plugin.interrupt_turn(2, api)) == ""


class TestCompaction:
    def test_context_tokens_sums_fresh_and_cached_input(self):
        assert claude_agent.context_tokens(
            {"input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1}) == 16

    def test_context_tokens_of_nothing_is_none(self):
        assert claude_agent.context_tokens(None) is None
        assert claude_agent.context_tokens({"output_tokens": 9}) is None

    def test_an_oversized_context_is_compacted_before_the_next_turn(self, plugin_module, monkeypatch):
        monkeypatch.setattr(plugin_module, "COMPACT_CONTEXT_TOKENS", 10)
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()

        async def main():
            session = plugin.session(4)
            await session.ensure_connected(api.as_listener_api())
            session.last_context_tokens = 999
            await session.maybe_compact_if_oversized(api.as_listener_api())
            await session.disconnect()
            return session

        session = asyncio.run(main())
        assert FakeClaudeSDKClient.instances[0].queries == ["/compact"]
        assert session.last_context_tokens == 0
        # A compaction's own output is bookkeeping, not a reply to anyone.
        assert api.texts() == []

    def test_an_idle_chat_is_compacted_and_disconnected(self, plugin_module, monkeypatch):
        monkeypatch.setattr(plugin_module, "IDLE_COMPACT_SECONDS", 0)
        plugin = plugin_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
        api = FakeListenerAPI()

        async def main():
            session = plugin.session(6)
            await session.ensure_connected(api.as_listener_api())
            await session.maybe_compact_if_idle(api.as_listener_api())
            return session

        session = asyncio.run(main())
        assert FakeClaudeSDKClient.instances[0].queries == ["/compact"]
        assert FakeClaudeSDKClient.instances[0].disconnected is True
        assert session.client is None
