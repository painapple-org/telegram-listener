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
