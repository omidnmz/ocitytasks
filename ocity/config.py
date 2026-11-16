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


WEEKDAYS = {name: i for i, name in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
)}


def _parse_weekdays(value: str) -> list[int]:
    return [WEEKDAYS[v.strip().lower()] for v in value.split(",") if v.strip()]


def _parse_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


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
    # The weekly content cycle: plan day, then batch filming on production days
    plan_day: int = 3  # thursday
    production_days: list[int] = field(default_factory=lambda: [5, 1])  # saturday, tuesday
    review_day: int = 4  # friday: weekly review with the morning brief
    # The marketing -> Media Hub line: plans on the marketing day, filming and editing in batches
    marketing_day: int = 4  # friday
    film_days: list[int] = field(default_factory=lambda: [6])  # sunday
    edit_days: list[int] = field(default_factory=lambda: [0, 1])  # monday, tuesday
    story_time: time = time(11, 0)  # daily stories, while the Mayor is in the shop (11-14)
    eve_time: time = time(21, 0)  # City Hall's heads-up for tomorrow's big jobs
    # Buildings in the order the Mayor wants to grow them
    building_priority: list[str] = field(default_factory=lambda: ["media", "retail", "smarto", "marketing"])
    db_path: str = "data/ocity.db"
    # Mini App: the public https address Telegram opens, and where the bot's web server listens
    webapp_url: str = ""
    site_url: str = ""  # the same city as a plain website, e.g. https://city.modiret.ir (login by Telegram link)
    web_host: str = "127.0.0.1"
    web_port: int = 8080

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


def load_env_file(path: str = ".env") -> None:
    """Read KEY=value lines from .env into the environment (existing variables win)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load() -> Config:
    load_env_file()
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
        plan_day=_parse_weekdays(env("PLAN_DAY", "thursday"))[0],
        production_days=_parse_weekdays(env("PRODUCTION_DAYS", "saturday,tuesday")),
        review_day=_parse_weekdays(env("REVIEW_DAY", "friday"))[0],
        marketing_day=_parse_weekdays(env("MARKETING_DAY", "friday"))[0],
        film_days=_parse_weekdays(env("FILM_DAYS", "sunday")),
        edit_days=_parse_weekdays(env("EDIT_DAYS", "monday,tuesday")),
        story_time=_parse_time(env("STORY_TIME", "11:00")),
        eve_time=_parse_time(env("EVE_TIME", "21:00")),
        building_priority=_parse_list(env("BUILDING_PRIORITY", "media,retail,smarto,marketing")),
        db_path=env("DB_PATH", "data/ocity.db"),
        webapp_url=env("WEBAPP_URL", "").strip().rstrip("/"),
        site_url=env("SITE_URL", "").strip().rstrip("/"),
        web_host=env("WEB_HOST", "127.0.0.1"),
        web_port=int(env("WEB_PORT", "8080") or 8080),
    )
