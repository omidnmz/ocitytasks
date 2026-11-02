"""Claude: understands the Mayor's messages and writes the briefs.

Without an API key the bot still works: messages become plain City Hall
tasks and briefs use simple templates.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from .buildings import BUILDINGS, BUILDING_KEYS

log = logging.getLogger(__name__)

BuildingKey = Literal[BUILDING_KEYS]  # type: ignore[valid-type]

BUILDING_GUIDE = "\n".join(f"- {b.key}: {b.name}" for b in BUILDINGS.values())

SYSTEM = f"""You are the City Hall of OCity, the Mayor's personal COO living in Telegram.
OCity is the Mayor's life and businesses drawn as a city: each area is a building,
and every finished task adds a floor. The Mayor manages a Crocs store (Kish, main
branch Damoon, branches Rasht and Karaj), runs SmartO (a website of management
panels for businesses), a Fitness OS Telegram mini app, a trading bot, Media Hub
content for all businesses, and medical/dental equipment technician work.

The Mayor is busy, often interrupted by customers, and writes in Persian.
Always answer in short, warm, natural Persian. Be action-first. Never lecture.

Buildings (use the key):
{BUILDING_GUIDE}

Boards: today (do today or soon), waiting (waiting on someone else),
construction (longer project work, no fixed day)."""


class TaskIn(BaseModel):
    title: str = Field(description="Short task title in Persian")
    building: BuildingKey
    board: Literal["today", "waiting", "construction"]
    priority: Literal["normal", "high"] = Field(
        description="high when it affects income, a customer, or a deadline"
    )
    kind: Optional[str] = Field(
        description="Short lowercase English category for learning habits, e.g. woocommerce, content, packing, follow_up, filming"
    )
    due_date: Optional[str] = Field(description="YYYY-MM-DD in the Mayor's timezone, or null")
    due_time: Optional[str] = Field(description="HH:MM 24h, only if the Mayor named a time")
    estimate_min: Optional[int] = Field(description="Minutes, only if the Mayor said or it is obvious")
    delegated: bool = Field(description="True if this is for staff to do")


class MessagePlan(BaseModel):
    reply: str = Field(description="Short Persian reply to the Mayor")
    add_tasks: list[TaskIn]
    complete_task_ids: list[int] = Field(description="Ids of open tasks the Mayor says are done")
    add_rules: list[str] = Field(
        description="Lasting preferences about how the Mayor works, e.g. 'no website work on Fridays'"
    )


class ContentItem(BaseModel):
    date: str = Field(description="Publish date YYYY-MM-DD")
    business: str
    title: str
    format: str = Field(description="reel, story, post, motion, ...")
    prep: Optional[str] = Field(description="Products or things to prepare before filming")
    script: Optional[str] = Field(
        default=None, description="The script, shot list and caption for this piece, copied as written"
    )


class ContentPlan(BaseModel):
    items: list[ContentItem]
    reply: str = Field(description="Short Persian summary of what was scheduled")


class AI:
    def __init__(self, api_key: str, model: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _parse(self, schema, system_extra: str, user: str):
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM + "\n\n" + system_extra,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": "low"},
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined the request")
        return response.parsed_output

    def _text(self, system_extra: str, user: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM + "\n\n" + system_extra,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low"},
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined the request")
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def understand(self, message: str, context: str, now_local: datetime) -> MessagePlan:
        extra = (
            f"Now: {now_local:%Y-%m-%d %A %H:%M} (Mayor's local time).\n"
            "Turn the Mayor's message into actions. A message can add several tasks, "
            "mark tasks done, or state a lasting preference. If it is only a question, "
            "answer it in `reply` and add nothing.\n\n" + context
        )
        return self._parse(MessagePlan, extra, message)

    def content_plan(self, plan_text: str, now_local: datetime) -> ContentPlan:
        extra = (
            f"Now: {now_local:%Y-%m-%d %A}. The Mayor pasted a content plan or a table of "
            "ready content. Extract every content piece with a concrete publish date. "
            "If the plan has no dates, spread items one per day starting tomorrow."
        )
        return self._parse(ContentPlan, extra, plan_text)

    def brief(self, kind: Literal["morning", "evening"], context: str, now_local: datetime) -> str:
        if kind == "morning":
            ask = (
                "Write the morning check-in, readable in under a minute: at most 3 main "
                "tasks for today (income first), 1 thing waiting on others, overdue items "
                "if any, and one thing NOT to work on today. If there are staff tasks, "
                "ask what to delegate. Use short lines and building emojis."
            )
        else:
            ask = (
                "Write the evening wrap-up in 2-4 short lines: what got built today, what "
                "moves to tomorrow, and one encouraging line. No questions except, if "
                "useful, which task to hand to staff tomorrow."
            )
        return self._text(f"Now: {now_local:%Y-%m-%d %A %H:%M}.\n{ask}", context)


def make_ai(api_key: str, model: str) -> AI | None:
    return AI(api_key, model) if api_key else None
