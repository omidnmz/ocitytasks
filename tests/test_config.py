import os

from ocity import config


def test_env_file_is_read_and_real_env_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "﻿# comment\nTELEGRAM_BOT_TOKEN=abc:123\nMORNING_TIME=09:15\nOWNER_CHAT_ID=\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    for key in ("TELEGRAM_BOT_TOKEN", "MORNING_TIME", "OWNER_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MORNING_TIME", "08:00")
    cfg = config.load()
    assert cfg.telegram_token == "abc:123"
    assert cfg.morning_time.hour == 8
    assert cfg.owner_chat_id is None
    for key in ("TELEGRAM_BOT_TOKEN", "OWNER_CHAT_ID"):
        os.environ.pop(key, None)
