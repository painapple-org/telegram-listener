import importlib
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own scratch data dir, and modules that computed
    path constants at import time (TRANSCRIPT_FILE, etc) get reloaded so
    those constants point inside it."""
    monkeypatch.setenv("TELEGRAM_LISTENER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SPOOR_HOME", raising=False)

    import telegram_listener.common as common
    import telegram_listener.listener as listener

    importlib.reload(common)
    importlib.reload(listener)
    yield tmp_path
