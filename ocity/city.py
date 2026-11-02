"""City Hall's brain, kept free of Telegram so it can be tested.

`CityHall.tick(now)` runs every minute and returns the messages to send:
the morning brief, task nudges, content sprint follow-ups and the evening
wrap-up. Button presses come back through `done`, `snooze`, `block`, `delegate`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import learning
from .ai import AI, ContentItem
from .buildings import BUILDINGS, CONTENT_SPRINT_STEPS, STARTER_TASKS, label
from .config import Config
from .db import DB, Task, parse_iso

log = logging.getLogger(__name__)

RENUDGE_HIGH_AFTER = timedelta(hours=2)
SPRINT_DAYS = 3


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _parse_row(line: str) -> ContentItem | None:
    cells = [c.strip() for c in line.translate(PERSIAN_DIGITS).strip().lstrip("#").strip().strip("|").split("|")]
    if len(cells) < 4:
        return None
    try:
        day = date.fromisoformat(cells[0].replace("/", "-"))
    except ValueError:
        return None  # header or separator row
    return ContentItem(
        date=day.isoformat(), business=cells[1], title=cells[2], format=cells[3],
        prep=cells[4] if len(cells) > 4 and cells[4] else None,
    )


def parse_plan_table(text: str) -> list[ContentItem]:
    """Read "date | business | title | format | prep" lines.

    Any lines under an item line, up to the next item, are that item's script
    (shot list, voice-over, caption...). Lines before the first item are ignored.
    """
    items: list[ContentItem] = []
    script: list[str] = []

    def close() -> None:
        if items and script:
            items[-1].script = "\n".join(script).strip() or None
        script.clear()

    for line in text.splitlines():
        item = _parse_row(line)
        if item:
            close()
            items.append(item)
        elif items:
            script.append(line.rstrip())
    close()
    return items


@dataclass
class Outgoing:
    text: str
    task_id: int | None = None  # attach snooze/done buttons for one task
    done_buttons: list[Task] = field(default_factory=list)  # one ✅ per task


@dataclass
class Result:
    reply: str
    added: list[Task] = field(default_factory=list)


class CityHall:
    def __init__(self, db: DB, cfg: Config, ai: AI | None = None):
        self.db = db
        self.cfg = cfg
        self.ai = ai
        self.tz = cfg.timezone

    # --- time helpers ----------------------------------------------------
    def local(self, now: datetime) -> datetime:
        return now.astimezone(self.tz)

    def at_local(self, day: date, t: time) -> datetime:
        return datetime.combine(day, t, tzinfo=self.tz)

    def out_of_quiet(self, when: datetime) -> datetime:
        """Move a nudge that falls in a quiet window (busy shop hours) to its end."""
        local = self.local(when)
        for start, end in self.cfg.quiet_windows:
            if start <= local.time() < end:
                return self.at_local(local.date(), end)
        return when

    def profile(self) -> dict[str, learning.Pattern]:
        return learning.build_profile(self.db.done_tasks(), self.tz)

    def first_nudge(self, task: Task, now: datetime) -> datetime | None:
        """When to first follow up on a task: its own time, else the learned usual time."""
        if task.status != "open" or (task.board != "today" and not task.due_date):
            return None
        today = self.local(now).date()
        day = max(date.fromisoformat(task.due_date) if task.due_date else today, today)
        if task.due_time:
            t = time.fromisoformat(task.due_time)
        else:
            t = learning.suggest_time(task, self.profile())
            if t is None:
                return None
        when = self.at_local(day, t)
        if when <= now:
            if not (day == today and task.due_time):
                return None
            when = now + timedelta(minutes=1)
        return self.out_of_quiet(when)

    # --- setup -----------------------------------------------------------
    def seed(self, now: datetime) -> None:
        if self.db.get_state("seeded"):
            return
        today = self.local(now).date().isoformat()
        for title, building, board, priority in STARTER_TASKS:
            self.db.add_task(
                title, building, board, priority=priority,
                due_date=today if board == "today" else None, now=now,
            )
        self.start_content_sprint(now)
        self.db.set_state("seeded", "1")

    def start_content_sprint(self, now: datetime) -> int:
        mission_id = self.db.add_mission("اسپرینت محتوا", now, now + timedelta(days=SPRINT_DAYS))
        today = self.local(now).date()
        for i, step in enumerate(CONTENT_SPRINT_STEPS):
            due = today + timedelta(days=min(i // 2, SPRINT_DAYS - 1))
            self.db.add_task(
                step, "media", "today", priority="high", kind="content_planning",
                due_date=due.isoformat(), mission_id=mission_id, step_order=i, now=now,
            )
        return mission_id

    # --- adding work -----------------------------------------------------
    def add(self, title: str, building: str = "cityhall", board: str = "today", now: datetime | None = None, **kw) -> Task:
        now = now or datetime.now(self.tz)
        if building not in BUILDINGS:
            building = "cityhall"
        if board == "today" and not kw.get("due_date"):
            kw["due_date"] = self.local(now).date().isoformat()
        task = self.db.add_task(title, building, board, now=now, **kw)
        nudge = self.first_nudge(task, now)
        if nudge:
            self.db.update_task(task.id, next_nudge_at=nudge)
            task = self.db.get_task(task.id)
        return task

    def handle_message(self, text: str, now: datetime) -> Result:
        if not self.ai:
            task = self.add(text, now=now)
            return Result(f"ثبت شد توی {label(task.building)} ✅", [task])
        try:
            plan = self.ai.understand(text, self.context(now), self.local(now))
        except Exception:
            log.exception("Claude failed; saving the message as a plain task")
            task = self.add(text, now=now)
            return Result(f"ثبت شد توی {label(task.building)} ✅", [task])

        added = []
        for t in plan.add_tasks:
            added.append(self.add(
                t.title, t.building, t.board, now=now, priority=t.priority, kind=t.kind,
                due_date=t.due_date, due_time=t.due_time, estimate_min=t.estimate_min,
                delegated=t.delegated,
            ))
        for task_id in plan.complete_task_ids:
            if (task := self.db.get_task(task_id)) and task.status == "open":
                self.done(task_id, now)
        for rule in plan.add_rules:
            self.db.add_rule(rule)
        return Result(plan.reply, added)

    def import_content_plan(self, text: str, now: datetime) -> Result:
        items = parse_plan_table(text)
        if items:
            reply = f"{len(items)} محتوا از جدول خونده شد."
        elif self.ai:
            plan = self.ai.content_plan(text, self.local(now))
            items, reply = plan.items, plan.reply
        else:
            return Result(
                "این برنامه رو نتونستم بخونم. بدون کلید Claude، هر محتوا باید یه خط به این شکل باشه:\n"
                "2026-11-05 | کراکس | راهنمای سایز | ریل | دمپایی کلاسیک مشکی و سفید"
            )
        added = []
        today = self.local(now).date()
        for item in items:
            publish = date.fromisoformat(item.date)
            title = f"انتشار {item.format}: {item.title} ({item.business})"
            added.append(self.add(
                title, "media", "today", now=now, kind="publish", due_date=item.date,
                priority="high", notes=item.script,
            ))
            if item.script:
                film_day = max(publish - timedelta(days=1), today)
                added.append(self.add(
                    f"ضبط «{item.title}» ({item.format})", "media", "today", now=now,
                    kind="filming", due_date=film_day.isoformat(), notes=item.script,
                ))
            if item.prep:
                prep_day = max(publish - timedelta(days=2), today)
                added.append(self.add(
                    f"آماده‌سازی برای «{item.title}»: {item.prep}", "media", "today", now=now,
                    kind="content_prep", due_date=prep_day.isoformat(), delegated=True,
                ))
        self.db.set_state("content_plan", text[:20000])
        return Result(reply, added)

    # --- buttons ---------------------------------------------------------
    def done(self, task_id: int, now: datetime) -> Task:
        self.db.update_task(task_id, status="done", done_at=now, next_nudge_at=None)
        self.db.log(task_id, "done", now=now)
        return self.db.get_task(task_id)

    def set_duration(self, task_id: int, minutes: int) -> None:
        self.db.update_task(task_id, actual_min=minutes)
        self.db.log(task_id, "duration", minutes=minutes)

    def snooze(self, task_id: int, option: str, now: datetime) -> datetime:
        task = self.db.get_task(task_id)
        local = self.local(now)
        if option == "30":
            when = now + timedelta(minutes=30)
        elif option == "60":
            when = now + timedelta(hours=1)
        elif option == "tonight":
            evening = self.at_local(local.date(), self.cfg.evening_time) - timedelta(hours=2)
            when = evening if evening > now else now + timedelta(hours=1)
        else:  # tomorrow
            tomorrow = local.date() + timedelta(days=1)
            t = learning.suggest_time(task, self.profile()) or self.cfg.morning_time
            when = self.at_local(tomorrow, t)
            self.db.update_task(task_id, due_date=tomorrow.isoformat())
        when = self.out_of_quiet(when)
        self.db.update_task(task_id, next_nudge_at=when, postpone_count=task.postpone_count + 1)
        self.db.log(task_id, "snoozed", now=now, option=option)
        return when

    def block(self, task_id: int, now: datetime) -> None:
        self.db.update_task(task_id, board="waiting", next_nudge_at=None)
        self.db.log(task_id, "blocked", now=now)

    def delegate(self, task_id: int, now: datetime) -> None:
        self.db.update_task(task_id, delegated=1, board="waiting", next_nudge_at=None)
        self.db.log(task_id, "delegated", now=now)

    # --- the minute tick -------------------------------------------------
    def tick(self, now: datetime) -> list[Outgoing]:
        out: list[Outgoing] = []
        local = self.local(now)
        today = local.date().isoformat()

        if local.time() >= self.cfg.morning_time and self.db.get_state("last_morning") != today:
            self.db.set_state("last_morning", today)
            out.append(Outgoing(self.morning_brief(now)))
            out.extend(self.todays_scripts(now))

        for task in self.db.tasks_due_for_nudge(now):
            out.append(Outgoing(self.nudge_text(task, now), task_id=task.id))
            renudge = now + RENUDGE_HIGH_AFTER if task.priority == "high" and task.nudge_count == 0 else None
            self.db.update_task(task.id, nudge_count=task.nudge_count + 1, next_nudge_at=renudge)
            self.db.log(task.id, "nudged", now=now)

        out.extend(self.mission_followups(now))
        out.extend(self.checkins(now))

        if local.time() >= self.cfg.evening_time and self.db.get_state("last_evening") != today:
            self.db.set_state("last_evening", today)
            out.append(Outgoing(self.evening_wrap(now)))
        return out

    def todays_scripts(self, now: datetime) -> list[Outgoing]:
        """On the day a filming or publish task is due, hand over its script."""
        today = self.local(now).date().isoformat()
        return [
            Outgoing(self.script_text(t), task_id=t.id)
            for t in self.db.open_tasks()
            if t.notes and t.kind in ("filming", "publish") and t.due_date and t.due_date <= today
        ]

    def script_text(self, task: Task) -> str:
        body = task.notes or "برای این کار سناریویی ثبت نشده."
        return f"📝 {task.title}\n\n{body}"[:4000]

    def checkins(self, now: datetime) -> list[Outgoing]:
        """Midday/evening sweep over today's tasks that have no time of their own yet."""
        local = self.local(now)
        today = local.date().isoformat()
        for slot in self.cfg.checkin_times:
            key = f"checkin:{today}:{slot:%H%M}"
            if local.time() < slot or self.db.get_state(key):
                continue
            self.db.set_state(key, "1")
            if self.at_local(local.date(), slot) + timedelta(minutes=30) < now:
                continue
            items = [
                t for t in self.db.open_tasks()
                if t.board == "today" and (t.due_date or today) <= today
                and not t.next_nudge_at and t.mission_id is None
            ][:5]
            if items:
                lines = ["📋 از کارهای امروز کدوم انجام شد؟", ""]
                lines += [f"#{t.id} {BUILDINGS[t.building].emoji} {t.title}" for t in items]
                return [Outgoing("\n".join(lines), done_buttons=items)]
        return []

    def nudge_text(self, task: Task, now: datetime) -> str:
        overdue = task.due_date and task.due_date < self.local(now).date().isoformat()
        prefix = "⏰ عقب افتاده: " if overdue else ("🔁 دوباره: " if task.nudge_count else "")
        text = f"{prefix}{label(task.building)}\n{task.title}\nرسیدی؟"
        if task.notes:
            text += f"\n\n📝 {task.notes}"
        return text[:4000]

    def mission_followups(self, now: datetime) -> list[Outgoing]:
        out = []
        local = self.local(now)
        for m in self.db.active_missions(now):
            steps = self.db.mission_steps(m["id"])
            pending = [s for s in steps if s.status == "open"]
            if not pending:
                self.db.close_mission(m["id"])
                out.append(Outgoing(f"🎉 «{m['name']}» تموم شد! Media Hub چند طبقه بالا رفت."))
                continue
            for slot in self.cfg.mission_followups:
                key = f"mission:{m['id']}:{local.date()}:{slot:%H%M}"
                if local.time() >= slot and not self.db.get_state(key):
                    self.db.set_state(key, "1")
                    # only the latest passed slot speaks, so a restart does not burst messages
                    if (self.at_local(local.date(), slot) + timedelta(minutes=30)) < now:
                        continue
                    step = pending[0]
                    done_n = len(steps) - len(pending)
                    out.append(Outgoing(
                        f"🎬 {m['name']} ({done_n}/{len(steps)})\nقدم بعدی: {step.title}",
                        task_id=step.id,
                    ))
        return out

    # --- briefs ----------------------------------------------------------
    def context(self, now: datetime) -> str:
        today = self.local(now).date().isoformat()
        lines = ["Open tasks (id | building | board | due | flags | title):"]
        for t in self.db.open_tasks():
            flags = []
            if t.priority == "high":
                flags.append("high")
            if t.due_date and t.due_date < today:
                flags.append("OVERDUE")
            if t.delegated:
                flags.append("staff")
            if t.postpone_count:
                flags.append(f"postponed x{t.postpone_count}")
            due = f"{t.due_date or '-'} {t.due_time or ''}".strip()
            lines.append(f"{t.id} | {t.building} | {t.board} | {due} | {','.join(flags)} | {t.title}")
        start = self.at_local(self.local(now).date(), time(0, 0))
        done = self.db.done_between(start, now + timedelta(seconds=1))
        if done:
            lines.append("\nDone today: " + "; ".join(t.title for t in done))
        lines.append("\nWork profile:\n" + learning.describe(self.profile()))
        postponed = learning.often_postponed(self.profile())
        if postponed:
            lines.append("Often postponed: " + ", ".join(postponed))
        rules = self.db.rules()
        if rules:
            lines.append("\nMayor's rules:\n" + "\n".join(f"- {r}" for r in rules))
        return "\n".join(lines)

    def morning_brief(self, now: datetime) -> str:
        if self.ai:
            try:
                return self.ai.brief("morning", self.context(now), self.local(now))
            except Exception:
                log.exception("Claude brief failed; using template")
        return self.template_morning(now)

    def template_morning(self, now: datetime) -> str:
        today = self.local(now).date().isoformat()
        open_ = self.db.open_tasks()
        due = [t for t in open_ if t.board == "today" and (t.due_date or today) <= today]
        overdue = [t for t in due if t.due_date and t.due_date < today]
        waiting = [t for t in open_ if t.board == "waiting"]
        lines = ["☀️ صبح بخیر شهردار!", "", "سه کار اصلی امروز:"]
        lines += [f"{i}. {label(t.building)}: {t.title}" for i, t in enumerate(due[:3], 1)] or ["امروز کار ثبت‌شده‌ای نداری."]
        if overdue:
            lines += ["", f"⏰ {len(overdue)} کار عقب افتاده داری."]
        if waiting:
            lines += ["", f"⏳ منتظر بقیه: {waiting[0].title}"]
        return "\n".join(lines)

    def evening_wrap(self, now: datetime) -> str:
        local = self.local(now)
        start = self.at_local(local.date(), time(0, 0))
        tomorrow = (local.date() + timedelta(days=1)).isoformat()
        today = local.date().isoformat()
        carried = 0
        for t in self.db.open_tasks():
            if t.board == "today" and t.due_date and t.due_date <= today:
                self.db.update_task(t.id, due_date=tomorrow, postpone_count=t.postpone_count + 1)
                self.db.log(t.id, "carried", now=now)
                carried += 1
        if self.ai:
            try:
                return self.ai.brief("evening", self.context(now), local)
            except Exception:
                log.exception("Claude wrap failed; using template")
        done = self.db.done_between(start, now + timedelta(seconds=1))
        return (
            f"🌙 جمع‌بندی امروز: {len(done)} طبقه ساخته شد"
            + (f" و {carried} کار رفت برای فردا." if carried else ".")
        )

    # --- views -----------------------------------------------------------
    def city_view(self, now: datetime) -> str:
        floors = self.db.floors_by_building()
        today = self.local(now).date().isoformat()
        open_ = self.db.open_tasks()
        lines = ["🏙 شهر تو امروز:", ""]
        for key, b in BUILDINGS.items():
            n = floors.get(key, 0)
            overdue = sum(1 for t in open_ if t.building == key and t.due_date and t.due_date < today)
            pending = sum(1 for t in open_ if t.building == key)
            tower = "▮" * min(n, 15) or "▫"
            extra = " 🚧 داربست" if overdue else ""
            lines.append(f"{b.emoji} {b.name}: {tower} {n} طبقه، {pending} کار باز{extra}")
        return "\n".join(lines)

    def tasks_view(self, now: datetime) -> str:
        today = self.local(now).date().isoformat()
        boards = {"today": "📌 امروز", "waiting": "⏳ در انتظار", "construction": "🏗 در حال ساخت"}
        lines = []
        open_ = self.db.open_tasks()
        for board, title in boards.items():
            items = [t for t in open_ if t.board == board]
            if not items:
                continue
            lines.append(f"\n{title}")
            for t in items:
                mark = "⏰ " if t.due_date and t.due_date < today else ""
                staff = " 👥" if t.delegated else ""
                note = " 📝" if t.notes else ""
                lines.append(f"{mark}#{t.id} {BUILDINGS[t.building].emoji} {t.title}{staff}{note}")
        text = "\n".join(lines).strip() or "هیچ کار بازی نداری 🎉"
        if any(t.notes for t in open_):
            text += "\n\n📝 یعنی سناریو داره: /script شماره"
        return text
