"""Settings, read once from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo


def _parse_time(value: str) -> time:
    hour, minute = value.strip().split(":")
    return time(int(hour), int(minute))


def _parse_times(value: str) -> list[time]:
    return [_parse_time(v) for v in value.split(",") if v.strip()]


def _parse_windows(value: str) -> list[tuple[time, time]]:
    """"14:30-16:00,19:00-20:00" -> [(14:30, 16:00), (19:00, 20:00)]"""
    windows = []
    for chunk in value.split(","):
        if "-" in chunk:
            start, end = chunk.split("-")
            windows.append((_parse_time(start), _parse_time(end)))
    return windows


@dataclass
class Config:
    telegram_token: str = ""
    owner_chat_id: int | None = None
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Asia/Tehran"))
    morning_time: time = time(10, 30)
    evening_time: time = time(23, 0)
    mission_followups: list[time] = field(
        default_factory=lambda: [time(12, 30), time(17, 0), time(21, 30)]
    )
    checkin_times: list[time] = field(default_factory=lambda: [time(14, 0), time(20, 0)])
    quiet_windows: list[tuple[time, time]] = field(default_factory=list)
    db_path: str = "data/ocity.db"

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


def load() -> Config:
    env = os.environ.get
    owner = env("OWNER_CHAT_ID", "").strip()
    return Config(
        telegram_token=env("TELEGRAM_BOT_TOKEN", ""),
        owner_chat_id=int(owner) if owner else None,
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        claude_model=env("CLAUDE_MODEL", "claude-opus-5-5"),
        timezone=ZoneInfo(env("TIMEZONE", "Asia/Tehran")),
        morning_time=_parse_time(env("MORNING_TIME", "10:30")),
        evening_time=_parse_time(env("EVENING_TIME", "23:00")),
        mission_followups=_parse_times(env("MISSION_FOLLOWUPS", "12:30,17:00,21:30")),
        checkin_times=_parse_times(env("CHECKIN_TIMES", "14:00,20:00")),
        quiet_windows=_parse_windows(env("QUIET_WINDOWS", "")),
        db_path=env("DB_PATH", "data/ocity.db"),
    )
