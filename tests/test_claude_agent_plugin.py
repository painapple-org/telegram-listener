import asyncio

from plugins.claude_agent import ClaudeAgentPlugin
from telegram_listener.plugin import ListenerAPI, Turn
from tests.fakes import FakeClaudeSDKClient


def make_api(sent, reactions):
    return ListenerAPI(
        send_message=lambda chat_id, text: sent.append((chat_id, text)),
        send_typing=lambda chat_id: None,
        set_reaction=lambda chat_id, message_id, emoji: reactions.append((chat_id, message_id, emoji)),
        log=lambda message: None,
    )


def test_handle_turn_streams_reply_through_api(tmp_path):
    FakeClaudeSDKClient.instances.clear()
    plugin = ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
    sent, reactions = [], []
    api = make_api(sent, reactions)
    turn = Turn(chat_id=1, message_ids=[10], text="hello", sender_name="Xander", sender_id=42)

    asyncio.run(plugin.handle_turn(turn, api))

    assert sent == [(1, "(fake reply)")]
    assert len(FakeClaudeSDKClient.instances) == 1
    assert FakeClaudeSDKClient.instances[0].queries == ["hello"]


def test_session_id_persists_across_plugin_instances(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_LISTENER_DATA_DIR", str(tmp_path))
    import importlib
    import telegram_listener.common as common
    import plugins.claude_agent as claude_agent_module
    importlib.reload(common)
    importlib.reload(claude_agent_module)

    FakeClaudeSDKClient.instances.clear()
    sent, reactions = [], []
    api = make_api(sent, reactions)

    plugin_a = claude_agent_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
    turn = Turn(chat_id=5, message_ids=[1], text="first", sender_name="X", sender_id=1)
    asyncio.run(plugin_a.handle_turn(turn, api))
    first_session_id = FakeClaudeSDKClient.instances[0].options.session_id

    # A brand-new plugin instance (simulating a process restart) resumes
    # the same session for the same chat, instead of starting fresh.
    plugin_b = claude_agent_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
    turn2 = Turn(chat_id=5, message_ids=[2], text="second", sender_name="X", sender_id=1)
    asyncio.run(plugin_b.handle_turn(turn2, api))

    assert FakeClaudeSDKClient.instances[1].options.resume == first_session_id


def test_clear_command_drops_the_session(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_LISTENER_DATA_DIR", str(tmp_path))
    import importlib
    import telegram_listener.common as common
    import plugins.claude_agent as claude_agent_module
    importlib.reload(common)
    importlib.reload(claude_agent_module)

    FakeClaudeSDKClient.instances.clear()
    sent, reactions = [], []
    api = make_api(sent, reactions)
    plugin = claude_agent_module.ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)

    turn = Turn(chat_id=9, message_ids=[1], text="hi", sender_name="X", sender_id=1)
    asyncio.run(plugin.handle_turn(turn, api))

    handled = asyncio.run(plugin.handle_command(9, 2, "clear", "", api))
    assert handled is True
    assert FakeClaudeSDKClient.instances[0].disconnected is True
    assert "Cleared" in sent[-1][1]


def test_unrecognized_command_returns_false():
    plugin = ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
    sent, reactions = [], []
    api = make_api(sent, reactions)
    handled = asyncio.run(plugin.handle_command(1, 1, "notmine", "", api))
    assert handled is False


def test_describe_commands_lists_plugin_owned_commands():
    plugin = ClaudeAgentPlugin(client_factory=FakeClaudeSDKClient)
    names = {c["command"] for c in plugin.describe_commands()}
    assert {"clear", "compact"} <= names
