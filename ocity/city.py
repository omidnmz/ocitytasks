"""City Hall's brain, kept free of Telegram so it can be tested.

`CityHall.tick(now)` runs every minute and returns the messages to send:
the morning brief, task nudges, content sprint follow-ups and the evening
wrap-up. Button presses come back through `done`, `snooze`, `block`, `delegate`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import capture, learning, pipeline
from .ai import AI, ContentItem
from .buildings import (BUILDINGS, CONTENT_SPRINT_STEPS, DEFAULT_BUILDINGS, DISTRICTS, STARTER_TASKS, configure,
                        label)
from .config import Config
from .db import DB, Task, parse_iso

log = logging.getLogger(__name__)

RENUDGE_HIGH_AFTER = timedelta(hours=2)
SPRINT_DAYS = 3
STUCK_AFTER = 3  # postpones before City Hall offers to shrink or hand off a task
AWAY_AFTER = timedelta(hours=3)  # after the morning brief, ping if the Mayor has not shown up
RESCUE_BEFORE_EVENING = timedelta(hours=2)  # nothing done yet? offer one small task


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
    stuck: bool = False  # the task keeps getting postponed: offer to shrink, hand off or drop it
    done_buttons: list[Task] = field(default_factory=list)  # one ✅ per task
    start_kind: str | None = None  # a «start filming / editing» button for this station of the content line


@dataclass
class Result:
    reply: str
    added: list[Task] = field(default_factory=list)
    ask_building: bool = False  # could not tell which building: show the building picker
    ask_when: bool = False  # no time given: ask when to remind
    ask_times: list[str] = field(default_factory=list)  # kinds of work whose usual time City Hall must ask


def clean_emoji(s: str | None) -> str:
    """A building's sign: a few characters, nothing that could be read as markup."""
    return "".join(c for c in (s or "") if c not in "<>&\"'").strip()[:4]


class CityHall:
    def __init__(self, db: DB, cfg: Config, ai: AI | None = None):
        self.db = db
        self.cfg = cfg
        self.ai = ai
        self.tz = cfg.timezone
        self.load_buildings()
        self.move_into_districts()
        self.start_pipeline()
        self.drop_old_content_plan()

    # --- the Mayor's own building settings --------------------------------
    def building_settings(self) -> dict[str, dict]:
        return json.loads(self.db.get_state("buildings") or "{}")

    def load_buildings(self) -> None:
        configure(self.building_settings())
        stored = self.db.get_state("building_priority")
        if stored is not None:
            self.cfg.building_priority = [k for k in json.loads(stored) if k in BUILDINGS]

    def floors(self) -> dict[str, int]:
        """Height of every building: what it already had, plus one floor per finished task."""
        done = self.db.floors_by_building()
        done["cityhall"] = done.get("cityhall", 0) + int(self.db.get_state("checkin_days") or 0)
        return {key: b.base + done.get(key, 0) for key, b in BUILDINGS.items()}

    def move_into_districts(self) -> None:
        """Once: open tasks that belong to the new product buildings move there (Retail OS, Technician OS, courses)."""
        if self.db.get_state("districts_v1"):
            return
        sources = {"retailos": ("retail", "smarto"), "techos": ("technician", "smarto"), "knowledge": ("technician",)}
        for t in self.db.open_tasks():
            target = capture.guess_building(t.title)
            if target in sources and t.building in sources[target]:
                self.db.update_task(t.id, building=target)
                log.info("Moved task %s to %s", t.id, target)
        self.db.set_state("districts_v1", "1")

    def start_pipeline(self) -> None:
        """Once: the old content sprint steps go (planning now lives in the marketing tower),
        and each brand page gets its first «plan next week» task."""
        if self.db.get_state("pipeline_v1"):
            return
        now = datetime.now(self.tz)
        for t in self.db.open_tasks():
            if (t.mission_id is not None and t.building == "media" and t.title in CONTENT_SPRINT_STEPS) or \
                    (t.building == "marketing" and t.kind != "mkt_plan" and t.title.startswith("نوشتن برنامه مارکتینگ")):
                self.db.update_task(t.id, status="dropped")
                self.db.log(t.id, "dropped", now=now, reason="pipeline")
        for m in self.db.active_missions(now):
            if m["name"] == "اسپرینت محتوا":
                self.db.close_mission(m["id"])
        if self.db.get_state("seeded"):
            self.ensure_marketing_tasks(now)
        self.db.set_state("pipeline_v1", "1")

    OLD_CONTENT_KINDS = ("publish", "filming", "editing", "content_prep")

    def drop_old_content_plan(self) -> int:
        """Once: the reels and posts from the old monthly content plan go. The weekly marketing plans
        replace them, and every task the new line makes carries its brand, so these are the ones without."""
        if self.db.get_state("pipeline_v2"):
            return 0
        now = datetime.now(self.tz)
        n = 0
        for t in self.db.open_tasks():
            if t.building == "media" and t.kind in self.OLD_CONTENT_KINDS and not t.brand:
                self.db.update_task(t.id, status="dropped")
                self.db.log(t.id, "dropped", now=now, reason="old content plan")
                n += 1
        self.db.set_state("pipeline_v2", "1")
        return n

    def update_building(self, key: str, name: str | None = None, emoji: str | None = None,
                        priority: bool | None = None, district: str | None = None) -> None:
        if key not in BUILDINGS:
            raise KeyError(key)
        settings = self.building_settings()
        entry = settings.setdefault(key, {})
        if name and name.strip():
            entry["name"] = name.strip()[:30]
        if emoji and clean_emoji(emoji):
            entry["emoji"] = clean_emoji(emoji)
        if district in DISTRICTS:
            entry["district"] = district
        self.db.set_state("buildings", json.dumps(settings, ensure_ascii=False))
        if priority is not None:
            order = [k for k in self.cfg.building_priority if k != key]
            if priority:
                order.append(key)
            self.cfg.building_priority = order
            self.db.set_state("building_priority", json.dumps(order))
        self.load_buildings()

    def add_building(self, name: str, emoji: str = "🏢", district: str = "tech") -> str:
        settings = self.building_settings()
        n = 1
        while f"c{n}" in BUILDINGS or f"c{n}" in settings:
            n += 1
        key = f"c{n}"
        settings[key] = {"name": name.strip()[:30] or "ساختمون جدید", "emoji": clean_emoji(emoji) or "🏢",
                         "district": district if district in DISTRICTS else "tech", "custom": True}
        self.db.set_state("buildings", json.dumps(settings, ensure_ascii=False))
        self.load_buildings()
        return key

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
        self.db.set_state("seeded", "1")
        self.ensure_marketing_tasks(now)

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

    def capture(self, text: str, now: datetime) -> Result:
        """A new thought, understood without Claude: guess the building, read any time in it."""
        local = self.local(now)
        building = capture.guess_building(text)
        when = capture.parse_when(text, local.date(), local.time())
        if building == "vault":
            task = self.add(text, "vault", "construction", now=now)
            return Result(f"💡 رفت توی {label('vault')}. توی مرور هفته یادت میارم.", [task])
        task = self.add(
            text, building or "cityhall", now=now,
            due_date=when.day.isoformat() if when.day else None,
            due_time=f"{when.at:%H:%M}" if when.at else None,
        )
        reply = f"ثبت شد توی {label(task.building)} ✅" if building else "ثبت شد ✅"
        if when.found:
            reply += f"\n⏰ یادآوری: {capture.describe_when(when.day, when.at, local.date())}"
        return Result(reply, [task], ask_building=building is None, ask_when=not when.found)

    def set_building(self, task_id: int, building: str) -> Task:
        if building in BUILDINGS:
            self.db.update_task(task_id, building=building)
        return self.db.get_task(task_id)

    def schedule(self, task_id: int, day: date | None, at: time | None, now: datetime) -> str:
        """Set when to remind about a task; returns how City Hall describes that time."""
        today = self.local(now).date()
        if day is None:
            self.db.update_task(task_id, board="construction", due_date=None, due_time=None, next_nudge_at=None)
        else:
            self.db.update_task(
                task_id, board="today", due_date=day.isoformat(),
                due_time=f"{at:%H:%M}" if at else None, next_nudge_at=None,
            )
            nudge = self.first_nudge(self.db.get_task(task_id), now)
            self.db.update_task(task_id, next_nudge_at=nudge)
        self.db.log(task_id, "scheduled", now=now, day=day.isoformat() if day else None)
        return capture.describe_when(day, at, today)

    def remind(self, task_id: int, option: str, now: datetime) -> str:
        """A reminder button: 1h, evening, tonight, tomorrow, week, none."""
        local = self.local(now)
        today = local.date()
        task = self.db.get_task(task_id)
        if option == "1h":
            soon = local + timedelta(hours=1)
            return self.schedule(task_id, soon.date(), soon.time().replace(second=0, microsecond=0), now)
        if option in ("evening", "tonight"):
            at = time(18, 0) if option == "evening" else time(21, 0)
            day = today if local.time() < at else today + timedelta(days=1)
            return self.schedule(task_id, day, at, now)
        if option == "tomorrow":
            at = learning.suggest_time(task, self.profile()) or time(11, 0)
            return self.schedule(task_id, today + timedelta(days=1), at, now)
        if option == "week":
            return self.schedule(task_id, today + timedelta(days=2), None, now)
        return self.schedule(task_id, None, None, now)

    def remind_from_text(self, task_id: int, text: str, now: datetime) -> str | None:
        """«پنجشنبه ساعت ۵» typed after the reminder question. None if no time was found."""
        local = self.local(now)
        when = capture.parse_when(text, local.date(), local.time())
        if not when.found:
            return None
        return self.schedule(task_id, when.day, when.at, now)

    def split(self, task_id: int, first_step: str, now: datetime) -> Task:
        """A stuck task becomes a small first step for today; the big one waits under construction."""
        big = self.db.get_task(task_id)
        self.db.update_task(task_id, board="construction", due_date=None, next_nudge_at=None)
        self.db.log(task_id, "split", now=now)
        step = self.add(f"{first_step} (قدم اول: {big.title})", big.building, now=now, kind=big.kind)
        if not step.next_nudge_at:
            soon = self.local(now) + timedelta(hours=1)
            self.schedule(step.id, soon.date(), soon.time().replace(second=0, microsecond=0), now)
        return self.db.get_task(step.id)

    def handle_message(self, text: str, now: datetime) -> Result:
        if not self.ai:
            return self.capture(text, now)
        try:
            plan = self.ai.understand(text, self.context(now), self.local(now))
        except Exception:
            log.exception("Claude failed; reading the message without it")
            return self.capture(text, now)

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
        ask = len(added) == 1 and added[0].board == "today" and not added[0].due_time
        return Result(plan.reply, added, ask_when=ask)

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
        open_by_key = {(t.title, t.kind): t for t in self.db.open_tasks() if t.building == "media"}

        def put(title: str, kind: str, due: date, **kw) -> Task:
            """Sending the same plan again updates its tasks instead of duplicating them."""
            existing = open_by_key.get((title, kind))
            if existing:
                self.db.update_task(existing.id, due_date=due.isoformat(), notes=kw.get("notes"))
                return existing
            task = self.add(title, "media", "today", now=now, kind=kind, due_date=due.isoformat(), **kw)
            open_by_key[(title, kind)] = task
            added.append(task)
            return task

        for item in items:
            publish = date.fromisoformat(item.date)
            film_day = self.production_day_for(publish, today)
            published = put(f"انتشار {item.format}: {item.title} ({item.business})", "publish", publish,
                            priority="high", notes=item.script)
            if item.script:  # the card waits on the content line until it is filmed and edited
                self.db.update_task(published.id, board="pipeline", next_nudge_at=None)
            if item.script:
                put(f"ضبط «{item.title}» ({item.format})", "filming", film_day,
                    notes=item.script, link_id=published.id)
            if item.prep:
                put(f"آماده‌سازی برای «{item.title}»: {item.prep}", "content_prep",
                    max(film_day - timedelta(days=1), today), delegated=True)
        self.db.set_state("content_plan", text[:20000])
        self.db.set_state("last_plan_import", today.isoformat())
        return Result(reply, added)

    def production_day_for(self, publish: date, today: date) -> date:
        """Film in batches: the last production day before publishing, else the day before."""
        days = self.cfg.production_days
        d = publish - timedelta(days=1)
        while d >= today:
            if d.weekday() in days:
                return d
            d -= timedelta(days=1)
        return max(publish - timedelta(days=1), today)

    def content_buffer(self, now: datetime) -> int:
        """Pieces already filmed and waiting to be published."""
        today = self.local(now).date().isoformat()
        filmed = {t.link_id for t in self.db.done_tasks() if t.kind == "filming" and t.link_id}
        return sum(
            1 for t in self.db.open_tasks()
            if t.kind == "publish" and t.id in filmed and (t.due_date or today) >= today
        )

    def buffer_line(self, now: datetime) -> str:
        n = self.content_buffer(now)
        state = "🟢 سالم" if n >= 3 else ("🟡 در حال جبران" if n == 2 else "🔴 اورژانسی")
        return f"📦 بافر محتوا: {n} آماده انتشار ({state})"

    # --- the weekly content cycle ----------------------------------------
    def weekly_cycle(self, now: datetime) -> list[Outgoing]:
        """Marketing day: each brand page gets its «plan next week» task and a ready prompt to paste.
        Keeps asking for two more days while a plan is missing."""
        out = []
        today = self.local(now).date()
        since = (today.weekday() - self.cfg.marketing_day) % 7
        if since == 0:
            self.ensure_marketing_tasks(now)
        waiting = self.marketing_tasks()
        if since <= 2 and waiting:
            names = "، ".join(BUILDINGS[k].name for k in waiting if k in BUILDINGS)
            head = ("📣 امروز روز برنامه‌ریزی مارکتینگه." if since == 0
                    else "📣 هنوز برنامه مارکتینگ هفته بعد نرسیده. بدون اون Media Hub کاری برای ساختن نداره.")
            out.append(Outgoing(
                f"🏛 تالار شهر: {head}\nمنتظر برنامه: {names}\n"
                "متن پایین رو توی چت مارکتینگ هر برند بچسبون و جوابش رو همینجا برام بفرست. "
                "خودم استوری‌ها، ریلزها و کارها رو بین ساختمون‌ها پخش می‌کنم."))
            prompts = dict(zip(pipeline.MARKETING_BRANDS, self.marketing_prompts(now)))
            out.extend(Outgoing(prompts[k]) for k in waiting if k in prompts)
        return out

    # --- the weekly content cycle: marketing plans, Media Hub produces ---
    # A reel moves through stations; only its current one is a task. The publish task is the content card
    # itself: it waits on the "pipeline" board until the piece is filmed and edited.
    STAGE_NAMES = {"content_prep": "🧰 آماده‌سازی", "filming": "🎥 ضبط", "editing": "✂️ تدوین",
                   "publish": "📤 انتشار", "story": "📱 استوری"}
    ASK_TIME_KINDS = ("filming", "editing", "publish")
    TIME_CHOICES = (("10:00", "صبح ۱۰"), ("13:00", "ظهر ۱۳"), ("17:00", "عصر ۱۷"), ("21:00", "شب ۲۱"))

    def kind_time(self, kind: str) -> str | None:
        if kind == "story":
            return self.db.get_state("kind_time:story") or f"{self.cfg.story_time:%H:%M}"
        return self.db.get_state(f"kind_time:{kind}")

    def set_kind_time(self, kind: str, at: str) -> int:
        """Remember when the Mayor does this kind of work, and apply it to what is already waiting."""
        self.db.set_state(f"kind_time:{kind}", at)
        n = 0
        for t in self.db.open_tasks():
            if t.kind == kind and not t.due_time:
                self.db.update_task(t.id, due_time=at)
                n += 1
        return n

    def next_day_in(self, days: list[int], start: date, before: date | None = None) -> date:
        """The first of these weekdays from `start` (before `before` if given), else `start` itself."""
        d = start
        for _ in range(14):
            if before and d >= before:
                break
            if d.weekday() in days:
                return d
            d += timedelta(days=1)
        return start if not before else min(start, before)

    def film_day_for(self, publish: date, today: date) -> date:
        """Film on the last filming day that still leaves a day to edit before publishing."""
        d = publish - timedelta(days=2)
        while d >= today:
            if d.weekday() in self.cfg.film_days:
                return d
            d -= timedelta(days=1)
        return max(min(publish - timedelta(days=1), today + timedelta(days=1)), today)

    def edit_day_for(self, publish: date, today: date) -> date:
        d = today
        while d < publish:
            if d.weekday() in self.cfg.edit_days:
                return d
            d += timedelta(days=1)
        return max(min(today + timedelta(days=1), publish), today)

    def _quiet_add(self, title: str, building: str, board: str, now: datetime, **kw) -> Task:
        """A pipeline task: City Hall announces these in batches, so no one-by-one nudge."""
        task = self.db.add_task(title, building, board, now=now, **kw)
        return task

    def marketing_tasks(self) -> dict[str, Task]:
        return {t.brand: t for t in self.db.open_tasks() if t.kind == "mkt_plan" and t.brand}

    def ensure_marketing_tasks(self, now: datetime) -> list[Task]:
        """One open «plan next week» task in the marketing tower per brand page."""
        have = self.marketing_tasks()
        today = self.local(now).date().isoformat()
        added = []
        for key in pipeline.MARKETING_BRANDS:
            if key in BUILDINGS and key not in have:
                added.append(self._quiet_add(f"📣 برنامه مارکتینگ هفته بعد: {BUILDINGS[key].name}", "marketing",
                                             "today", now, kind="mkt_plan", brand=key, due_date=today))
        return added

    def marketing_prompts(self, now: datetime) -> list[str]:
        today = self.local(now).date()
        start = today + timedelta(days=1)
        out = []
        for key in pipeline.MARKETING_BRANDS:
            if key not in BUILDINGS:
                continue
            meta = json.loads(self.db.get_state(f"plan:{key}") or "{}")
            out.append(pipeline.prompt(BUILDINGS[key].name, start, start + timedelta(days=6), meta.get("metric", ""),
                                       pipeline.REEL_QUOTA.get(key, "۲ تا ۵")))
        return out

    def import_marketing_plan(self, text: str, now: datetime) -> Result:
        plans = pipeline.parse(text)
        if not plans:
            return Result("قالب برنامه رو نشناختم. باید با «#برند: ...» شروع بشه.")
        today = self.local(now).date()
        open_plans = self.marketing_tasks()
        added: list[Task] = []
        lines = []
        film_days: set[date] = set()
        for plan in plans:
            key = plan.building or capture.guess_building(plan.brand) or "cityhall"
            if key not in BUILDINGS:
                key = "cityhall"
            name = BUILDINGS[key].name
            # the marketing tower's own job is done: it gets its floor
            order = open_plans.pop(key, None) or self._quiet_add(
                f"📣 برنامه مارکتینگ: {name}", "marketing", "today", now, kind="mkt_plan", brand=key,
                due_date=today.isoformat())
            self.db.update_task(order.id, notes=text[:4000])
            self.done(order.id, now)
            self.db.set_state(f"plan:{key}", json.dumps(
                {"goal": plan.goal, "audience": plan.audience, "message": plan.message, "metric": plan.metric,
                 "week": today.isoformat()}, ensure_ascii=False))
            story_at = self.kind_time("story")
            for s in plan.stories:
                if s.day < today:
                    continue
                added.append(self._quiet_add(f"📱 استوری {name}: {s.topic}", "media", "today", now, kind="story",
                                             brand=key, due_date=s.day.isoformat(), due_time=story_at,
                                             notes=s.note or None))
            for r in plan.reels:
                publish_day = max(r.day, today)
                card = self._quiet_add(f"📤 انتشار {r.format}: {r.title} ({name})", "media", "pipeline", now,
                                       kind="publish", brand=key, priority="high", due_date=publish_day.isoformat(),
                                       due_time=self.kind_time("publish"), notes=r.script or None)
                film = self.film_day_for(publish_day, today)
                film_days.add(film)
                added.append(self._quiet_add(f"🎥 ضبط {r.format}: {r.title} ({name})", "media", "today", now,
                                             kind="filming", brand=key, due_date=film.isoformat(),
                                             due_time=self.kind_time("filming"), notes=r.script or None,
                                             link_id=card.id))
                if r.prep:
                    added.append(self._quiet_add(f"🧰 آماده‌سازی «{r.title}»: {r.prep}", "media", "today", now,
                                                 kind="content_prep", brand=key, delegated=True, link_id=card.id,
                                                 due_date=max(film - timedelta(days=1), today).isoformat()))
            routed: dict[str, int] = {}
            for a in plan.tasks:
                part = a.part.replace("‌", " ")
                staff = "پرسنل" in part or "staff" in part.lower()
                target = key if ("محصول" in part or "سایت" in part or staff or not part) else (
                    capture.guess_building(part) or capture.guess_building(a.title) or key)
                if target not in BUILDINGS:
                    target = key
                added.append(self.add(a.title, target, "construction", now=now, brand=key, delegated=staff))
                routed[target] = routed.get(target, 0) + 1
            bits = [f"{len(plan.stories)} استوری", f"{len(plan.reels)} ریلز"]
            if plan.tasks:
                bits.append(f"{len(plan.tasks)} کار ({'، '.join(f'{label(k)} {n}' for k, n in routed.items())})")
            lines.append(f"• {label(key)}: " + "، ".join(bits))
        reply = ["📣 برج مارکتینگ برنامه‌ها رو تحویل داد و یه طبقه بالا رفت.", *lines]
        if film_days:
            days = "، ".join(capture.describe_when(d, None, today) for d in sorted(film_days))
            reply.append(f"\n🎬 Media Hub: فیلم‌برداری {days}. شب قبلش و همون روز از تالار شهر خبرت می‌کنم.")
        reply.append(f"📱 استوری‌ها هر روز ساعت {self.kind_time('story')} یادت میاد.")
        ask = [k for k in self.ASK_TIME_KINDS if not self.kind_time(k) and any(t.kind == k for t in added)]
        if any(t.kind == "filming" for t in added) and not self.kind_time("editing"):
            ask.append("editing")
        if any(t.kind == "filming" for t in added) and not self.kind_time("publish"):
            ask.append("publish")
        return Result("\n".join(reply), added, ask_times=list(dict.fromkeys(ask)))

    def advance(self, task: Task, now: datetime) -> str:
        """A station of the content line is done: open the next one. Returns what City Hall says next."""
        today = self.local(now).date()
        card = self.db.get_task(task.link_id) if task.link_id else None
        if task.kind == "filming" and card and card.status == "open":
            publish = date.fromisoformat(card.due_date) if card.due_date else today + timedelta(days=2)
            day = self.edit_day_for(publish, today)
            existing = [t for t in self.db.open_tasks() if t.kind == "editing" and t.link_id == card.id]
            if not existing:
                self._quiet_add(card.title.replace("📤 انتشار", "✂️ تدوین", 1), "media", "today", now,
                                kind="editing", brand=card.brand, due_date=day.isoformat(),
                                due_time=self.kind_time("editing"), notes=card.notes, link_id=card.id)
            at = self.kind_time("editing")
            return f"بعدی: ✂️ تدوین، {capture.describe_when(day, None, today)}{' ساعت ' + at if at else ''}"
        if task.kind == "editing" and card and card.status == "open":
            at = self.kind_time("publish")
            publish = date.fromisoformat(card.due_date) if card.due_date else today
            self.db.update_task(card.id, board="today", due_time=at or card.due_time)
            card = self.db.get_task(card.id)
            nudge = self.first_nudge(card, now)
            if nudge:
                self.db.update_task(card.id, next_nudge_at=nudge)
            return f"بعدی: 📤 انتشار، {capture.describe_when(publish, None, today)}{' ساعت ' + at if at else ''}"
        if task.kind == "publish":
            return "این محتوا کامل منتشر شد 🎉"
        return ""

    def stage_queue(self, kind: str, now: datetime) -> list[Task]:
        """Today's (and overdue) tasks of one station, nearest publish date first."""
        today = self.local(now).date().isoformat()
        cards = {t.id: t for t in self.db.open_tasks() if t.kind == "publish"}
        items = [t for t in self.db.open_tasks() if t.kind == kind and (t.due_date or today) <= today]
        return sorted(items, key=lambda t: ((cards.get(t.link_id).due_date if cards.get(t.link_id) else None)
                                            or t.due_date or "9999", t.id))

    def focus_text(self, task: Task, left: int) -> str:
        head = f"{self.STAGE_NAMES.get(task.kind, '')} ({left} تا مونده)\n{task.title}"
        return f"{head}\n\n📝 {task.notes}"[:4000] if task.notes else head

    def city_hall_calls(self, now: datetime) -> list[Outgoing]:
        """City Hall tells the Mayor what he has to do with his own hands: stories, filming, editing."""
        out = []
        local = self.local(now)
        today = local.date()
        iso = today.isoformat()
        # daily stories, while he is in the shop
        story_at = time.fromisoformat(self.kind_time("story"))
        stories = [t for t in self.db.open_tasks() if t.kind == "story" and t.due_date == iso]
        if stories and local.time() >= story_at and self.db.get_state("calls:stories") != iso:
            self.db.set_state("calls:stories", iso)
            lines = ["🏛 تالار شهر: استوری‌های امروز 📱"]
            lines += [f"• {t.title.replace('📱 استوری ', '')}" + (f"\n   {t.notes}" if t.notes else "") for t in stories]
            lines.append("هر کدوم رو گذاشتی ✅ بزن تا چراغش روشن بشه.")
            out.append(Outgoing("\n".join(lines), done_buttons=stories[:8]))
        again = (datetime.combine(today, story_at) + timedelta(hours=2)).time()
        if stories and local.time() >= again and self.db.get_state("calls:stories2") != iso:
            self.db.set_state("calls:stories2", iso)
            out.append(Outgoing(f"🏛 هنوز {len(stories)} استوری امروز مونده. یه دقیقه وقت بذار تا چراغ‌ها خاموش نمونن.",
                                done_buttons=stories[:8]))
        # the evening before a filming or editing day
        tomorrow = (today + timedelta(days=1)).isoformat()
        if local.time() >= self.cfg.eve_time and self.db.get_state("calls:eve") != iso:
            self.db.set_state("calls:eve", iso)
            for kind, what in (("filming", "فیلم‌برداری"), ("editing", "تدوین")):
                items = [t for t in self.db.open_tasks() if t.kind == kind and t.due_date == tomorrow]
                if items:
                    at = self.kind_time(kind)
                    out.append(Outgoing(
                        f"🏛 تالار شهر: فردا Media Hub روز {what} داره، {len(items)} محتوا"
                        f"{' از ساعت ' + at if at else ''}. وقتش رو خالی نگه دار."
                        + ("\nوسایل و محصول‌ها رو امشب آماده کن." if kind == "filming" else "")))
        # the day itself: a start button at the work's time
        for kind, what in (("filming", "فیلم‌برداری"), ("editing", "تدوین")):
            items = self.stage_queue(kind, now)
            at = self.kind_time(kind)
            start = time.fromisoformat(at) if at else self.cfg.morning_time
            if items and local.time() >= start and self.db.get_state(f"calls:{kind}") != iso:
                self.db.set_state(f"calls:{kind}", iso)
                out.append(Outgoing(f"🏛 تالار شهر: وقت {what} Media Hub‌ه 🎬\n{len(items)} محتوا منتظرته. "
                                    "دکمه شروع رو بزن تا یکی‌یکی با سناریوش بیاد.", start_kind=kind))
        return out

    def lit_buildings(self, now: datetime) -> set[str]:
        """Buildings with a light on today: something done in them, or for them (a story for the Crocs page)."""
        local = self.local(now)
        start = self.at_local(local.date(), time(0, 0))
        lit = set()
        for t in self.db.done_between(start, now + timedelta(seconds=1)):
            lit.add(t.building)
            if t.brand:
                lit.add(t.brand)
        if self.db.get_state("last_checkin_day") == local.date().isoformat():
            lit.add("cityhall")
        return lit & set(BUILDINGS)

    def lights_line(self, now: datetime) -> str:
        lit = self.lit_buildings(now)
        total = [k for k in BUILDINGS if k != "vault"]
        return f"💡 امروز {len([k for k in total if k in lit])} از {len(total)} ساختمون روشنه."

    def buffers(self, now: datetime) -> dict[str, int]:
        """Filmed pieces not yet published, per brand page."""
        today = self.local(now).date().isoformat()
        filmed = {t.link_id for t in self.db.done_tasks() if t.kind == "filming" and t.link_id}
        out: dict[str, int] = {}
        for t in self.db.open_tasks():
            if t.kind == "publish" and t.id in filmed and (t.due_date or today) >= today:
                out[t.brand or "media"] = out.get(t.brand or "media", 0) + 1
        return out

    def media_state(self, now: datetime) -> dict:
        cards, stories = self.media_board(now)
        return {"cards": cards, "stories": stories, "buffers": self.buffers(now),
                "times": {k: self.kind_time(k) for k in ("story", *self.ASK_TIME_KINDS)},
                "waiting_plans": sorted(self.marketing_tasks())}

    def media_board(self, now: datetime) -> tuple[list[dict], list[dict]]:
        """Every open content card and the station it is at, nearest publish first."""
        open_ = self.db.open_tasks()
        steps: dict[int, dict[str, Task]] = {}
        for t in open_:
            if t.link_id and t.kind in ("content_prep", "filming", "editing"):
                steps.setdefault(t.link_id, {})[t.kind] = t
        cards = []
        for c in open_:
            if c.kind != "publish":
                continue
            mine = steps.get(c.id, {})
            step = mine.get("editing") or mine.get("filming")
            stage = step.kind if step else ("publish" if c.board == "today" else "filming")
            cards.append({"id": c.id, "task": (step or c).id, "title": c.title.replace("📤 انتشار ", "", 1),
                          "brand": c.brand, "stage": stage, "publish": c.due_date,
                          "due": (step or c).due_date, "prep": "content_prep" in mine})
        today = self.local(now).date().isoformat()
        stories = [{"id": t.id, "title": t.title.replace("📱 استوری ", "", 1), "brand": t.brand, "due": t.due_date}
                   for t in open_ if t.kind == "story" and (t.due_date or today) >= today]
        cards.sort(key=lambda c: (c["publish"] or "9999", c["id"]))
        stories.sort(key=lambda s: (s["due"] or "", s["id"]))
        return cards, stories

    # --- buttons ---------------------------------------------------------
    def done(self, task_id: int, now: datetime) -> Task:
        """Mark a task done; a station of the content line opens the next one (see `next_step` on the result)."""
        self.db.update_task(task_id, status="done", done_at=now, next_nudge_at=None)
        self.db.log(task_id, "done", now=now)
        task = self.db.get_task(task_id)
        task.next_step = self.advance(task, now)
        return task

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

    def drop(self, task_id: int, now: datetime) -> None:
        self.db.update_task(task_id, status="dropped", next_nudge_at=None)
        self.db.log(task_id, "dropped", now=now)

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
            if local.weekday() == self.cfg.review_day:
                out.append(Outgoing(self.weekly_review(now)))
            out.extend(self.weekly_cycle(now))
            out.extend(self.todays_scripts(now))

        for task in self.db.tasks_due_for_nudge(now):
            stuck = task.postpone_count >= STUCK_AFTER
            out.append(Outgoing(self.nudge_text(task, now), task_id=task.id, stuck=stuck))
            renudge = now + RENUDGE_HIGH_AFTER if task.priority == "high" and task.nudge_count == 0 else None
            self.db.update_task(task.id, nudge_count=task.nudge_count + 1, next_nudge_at=renudge)
            self.db.log(task.id, "nudged", now=now)

        out.extend(self.mission_followups(now))
        out.extend(self.city_hall_calls(now))
        out.extend(self.checkins(now))
        out.extend(self.keep_the_day(now))

        if local.time() >= self.cfg.evening_time and self.db.get_state("last_evening") != today:
            self.db.set_state("last_evening", today)
            carried = self.carry_over(now)
            out.append(Outgoing(self.evening_wrap(now, carried), done_buttons=carried[:6]))
        return out

    # --- making sure the day is not lost ---------------------------------
    def seen(self, now: datetime) -> None:
        """The Mayor wrote or tapped something. Each day he shows up is a floor for City Hall."""
        self.db.set_state("last_seen", now.isoformat())
        day = self.local(now).date().isoformat()
        if self.db.get_state("last_checkin_day") != day:
            self.db.set_state("last_checkin_day", day)
            self.db.set_state("checkin_days", str(int(self.db.get_state("checkin_days") or 0) + 1))

    def done_days(self) -> set[date]:
        return {self.local(parse_iso(t.done_at)).date() for t in self.db.done_tasks() if t.done_at}

    def streak(self, now: datetime) -> int:
        """Days in a row with at least one floor built; today counts once something is done."""
        days = self.done_days()
        day = self.local(now).date()
        if day not in days:
            day -= timedelta(days=1)
        n = 0
        while day in days:
            n += 1
            day -= timedelta(days=1)
        return n

    def streak_line(self, now: datetime) -> str:
        n = self.streak(now)
        today_done = self.local(now).date() in self.done_days()
        if n == 0:
            return "🔥 زنجیره ساخت: امروز با یه کار کوچیک شروعش کن."
        if today_done:
            return f"🔥 زنجیره ساخت: {n} روز پشت سر هم"
        return f"🔥 زنجیره ساخت: {n} روز. امروز یه طبقه بساز که نشکنه."

    def top_task(self, now: datetime) -> Task | None:
        today = self.local(now).date().isoformat()
        due = [
            t for t in self.db.open_tasks()
            if t.board == "today" and (t.due_date or today) <= today and not t.delegated
        ]
        order = {k: i for i, k in enumerate(self.cfg.building_priority)}
        due.sort(key=lambda t: (t.priority != "high", order.get(t.building, 99), t.due_date or "", t.id))
        return due[0] if due else None

    def keep_the_day(self, now: datetime) -> list[Outgoing]:
        """Ping when the Mayor has not shown up by midday, and rescue a day with nothing built."""
        out = []
        local = self.local(now)
        today = local.date()
        morning = self.at_local(today, self.cfg.morning_time)
        last_seen = parse_iso(self.db.get_state("last_seen"))
        away_at = morning + AWAY_AFTER
        key = f"away:{today}"
        if now >= away_at and not self.db.get_state(key):
            self.db.set_state(key, "1")
            if now < away_at + timedelta(minutes=30) and (last_seen is None or last_seen < morning):
                top = self.top_task(now)
                text = "🏛 شهردار، امروز هنوز به شهرداری سر نزدی. شهر منتظر قدم امروزته."
                if top:
                    text += f"\n\nمهم‌ترین کار امروز:\n{label(top.building)}: {top.title}"
                out.append(Outgoing(text + "\n\nگزارش کامل: /tasks", task_id=top.id if top else None))
        rescue_at = self.at_local(today, self.cfg.evening_time) - RESCUE_BEFORE_EVENING
        key = f"rescue:{today}"
        if now >= rescue_at and not self.db.get_state(key):
            self.db.set_state(key, "1")
            if now < rescue_at + timedelta(minutes=30) and today not in self.done_days():
                top = self.top_task(now)
                if top:
                    n = self.streak(now)
                    keep = f" تا زنجیره {n} روزه‌ت نشکنه" if n else ""
                    out.append(Outgoing(
                        f"🧱 امروز هنوز هیچ طبقه‌ای ساخته نشده. فقط همین یکی{keep}:\n"
                        f"{label(top.building)}: {top.title}",
                        task_id=top.id,
                    ))
        return out

    def weekly_review(self, now: datetime) -> str:
        """Once a week: what each building got, which ones were left alone, ideas waiting."""
        local = self.local(now)
        start = self.at_local(local.date() - timedelta(days=7), time(0, 0))
        done = self.db.done_between(start, now + timedelta(seconds=1))
        per = {}
        for t in done:
            per[t.building] = per.get(t.building, 0) + 1
        open_ = self.db.open_tasks()
        lines = [f"📊 مرور هفته: {len(done)} طبقه ساخته شد", ""]
        for key, n in sorted(per.items(), key=lambda kv: -kv[1]):
            lines.append(f"{label(key)}: {n}")
        idle = [
            k for k in self.cfg.building_priority
            if k in BUILDINGS and not per.get(k) and any(t.building == k for t in open_)
        ]
        if idle:
            lines += ["", "🕸 این هفته دست نخوردن: " + "، ".join(label(k) for k in idle)]
        stuck = [t for t in open_ if t.postpone_count >= STUCK_AFTER][:3]
        if stuck:
            lines += ["", "🐢 بیشتر از همه عقب افتادن:"]
            lines += [f"#{t.id} {label(t.building)}: {t.title} ({t.postpone_count} بار)" for t in stuck]
        ideas = [t for t in open_ if t.building == "vault"]
        if ideas:
            lines += ["", f"💡 {len(ideas)} ایده توی گنجینه منتظره. اگه یکی‌ش وقتشه، بگو بیارمش توی کارها."]
        lines += ["", self.streak_line(now)]
        return "\n".join(lines)

    def todays_scripts(self, now: datetime) -> list[Outgoing]:
        """On the day a filming or publish task is due, hand over its script."""
        today = self.local(now).date().isoformat()
        return [
            Outgoing(self.script_text(t), task_id=t.id)
            for t in self.db.open_tasks()
            if t.notes and t.kind == "publish" and t.board == "today" and t.due_date and t.due_date <= today
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
        if task.postpone_count >= STUCK_AFTER:
            text += (f"\n\n🐢 این کار {task.postpone_count} بار عقب افتاده. "
                     "کوچیکش کنیم، بدیمش به پرسنل یا کلاً حذفش کنیم؟")
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
        lines.append("\nBuilding priority (grow first): " + ", ".join(self.cfg.building_priority))
        lines.append(f"Content buffer (filmed, not yet published): {self.content_buffer(now)}")
        lines.append(f"Build streak (days in a row with something done): {self.streak(now)}")
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
        return "☀️ صبح بخیر شهردار!\n" + self.streak_line(now) + "\n\n" + self.tasks_view(now)

    def carry_over(self, now: datetime) -> list[Task]:
        """Unfinished tasks of today move to tomorrow."""
        local = self.local(now)
        tomorrow = (local.date() + timedelta(days=1)).isoformat()
        today = local.date().isoformat()
        carried = []
        for t in self.db.open_tasks():
            if t.kind == "story" and t.due_date and t.due_date <= today:
                self.db.update_task(t.id, status="dropped")  # a story belongs to its day
                self.db.log(t.id, "missed", now=now)
                continue
            if t.board == "today" and t.due_date and t.due_date <= today:
                self.db.update_task(t.id, due_date=tomorrow, postpone_count=t.postpone_count + 1)
                self.db.log(t.id, "carried", now=now)
                carried.append(self.db.get_task(t.id))
        return carried

    def evening_wrap(self, now: datetime, carried: list[Task] | None = None) -> str:
        local = self.local(now)
        carried = carried or []
        if self.ai:
            try:
                return self.ai.brief("evening", self.context(now), local)
            except Exception:
                log.exception("Claude wrap failed; using template")
        start = self.at_local(local.date(), time(0, 0))
        done = self.db.done_between(start, now + timedelta(seconds=1))
        lines = [f"🌙 جمع‌بندی امروز: {len(done)} طبقه ساخته شد.", self.streak_line(now)]
        if carried:
            lines += ["", f"این‌ها رفت برای فردا ({len(carried)}). اگه یکی‌ش انجام شده، تیکش رو بزن:"]
            lines += [f"#{t.id} {BUILDINGS[t.building].emoji} {t.title}" for t in carried[:6]]
        return "\n".join(lines)

    # --- views -----------------------------------------------------------
    def city_view(self, now: datetime) -> str:
        floors = self.floors()
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
        """The Mayor's digest: what matters today, and one next step per building."""
        today = self.local(now).date().isoformat()
        open_ = self.db.open_tasks()

        def line(t: Task) -> str:
            staff = " 👥" if t.delegated else ""
            note = " 📝" if t.notes else ""
            return f"#{t.id} {BUILDINGS[t.building].emoji} {t.title}{staff}{note}"

        # of each mission, only the step that is up next
        seen_missions: set[int] = set()
        visible = []
        for t in sorted(open_, key=lambda t: (t.mission_id or 0, t.step_order or 0)):
            if t.mission_id is not None:
                if t.mission_id in seen_missions:
                    continue
                seen_missions.add(t.mission_id)
            visible.append(t)
        visible.sort(key=lambda t: (t.priority != "high", t.due_date or "9999", t.due_time or "", t.id))

        due = [t for t in visible if t.board == "today" and (t.due_date or today) <= today]
        overdue = [t for t in due if t.due_date and t.due_date < today]
        mine = [t for t in due if t not in overdue and not t.delegated]
        staff = [t for t in due if t not in overdue and t.delegated]
        waiting = [t for t in open_ if t.board == "waiting"]
        shown = {t.id for t in due}

        lines = ["🏛 گزارش شهرداری، امروز"]
        if overdue:
            lines += ["", f"⏰ عقب‌افتاده ({len(overdue)})"] + [line(t) for t in overdue[:5]]
        lines += ["", "📌 کارهای امروز تو"]
        lines += [line(t) for t in mine[:7]] or ["امروز کار ثبت‌شده‌ای نداری."]
        if len(mine) > 7:
            lines.append(f"... و {len(mine) - 7} کار دیگه")
        if staff:
            lines += ["", "👥 برای پرسنل"] + [line(t) for t in staff[:5]]
        if waiting:
            lines += ["", f"⏳ منتظر بقیه ({len(waiting)}): {waiting[0].title}"]

        nexts = []
        order = [k for k in self.cfg.building_priority if k in BUILDINGS]
        order += [k for k in BUILDINGS if k not in order]
        for key in order:
            b = BUILDINGS[key]
            queue = sorted(
                (t for t in visible if t.building == key and t.id not in shown and t.board != "waiting"),
                key=lambda t: (t.due_date or "9999", t.id),
            )
            if queue:
                t = queue[0]
                when = f" ({t.due_date})" if t.due_date else ""
                more = f"، {len(queue)} کار در صف" if len(queue) > 1 else ""
                nexts.append(f"{len(nexts) + 1}. {b.emoji} {b.name}: {t.title}{when}{more}")
        if nexts:
            lines += ["", "🏗 ساختمون‌های منتظر قدم بعدی (به ترتیب اولویت)"] + nexts
        if any(t.kind == "publish" for t in open_):
            lines += ["", self.buffer_line(now)]

        lines += ["", "همه کارها: /all · سناریو: /script شماره"]
        return "\n".join(lines)[:4000]

    def today_tasks(self, now: datetime) -> list[Task]:
        """Today's own work, overdue first, one step per mission, no staff tasks."""
        today = self.local(now).date().isoformat()
        seen_missions: set[int] = set()
        items = []
        for t in sorted(self.db.open_tasks(), key=lambda t: (t.mission_id or 0, t.step_order or 0)):
            if t.board != "today" or (t.due_date or today) > today or t.delegated:
                continue
            if t.mission_id is not None:
                if t.mission_id in seen_missions:
                    continue
                seen_missions.add(t.mission_id)
            items.append(t)
        order = {k: i for i, k in enumerate(self.cfg.building_priority)}
        items.sort(key=lambda t: (
            (t.due_date or today) >= today, t.priority != "high", t.due_time or "99",
            order.get(t.building, 99), t.id,
        ))
        return items

    def today_view(self, now: datetime, limit: int = 8) -> tuple[str, list[Task]]:
        """A short list of today's work; each task gets its own button."""
        today = self.local(now).date().isoformat()
        items = self.today_tasks(now)
        shown = items[:limit]
        if not items:
            lines = ["📌 کارهای امروز", "", "امروز کاری نمونده 🎉"]
            top = self.next_steps(now)[:1]
            if top:
                lines += ["", "اگه وقت داری، قدم بعدی:", top[0]]
            return "\n".join(lines), []
        lines = [f"📌 کارهای امروز ({len(items)})", ""]
        for t in shown:
            mark = "⏰ " if t.due_date and t.due_date < today else ""
            at = f" {t.due_time}" if t.due_time else ""
            lines.append(f"{mark}{BUILDINGS[t.building].emoji} {t.title}{at}")
        if len(items) > limit:
            lines.append(f"... و {len(items) - limit} کار دیگه (📋 همه کارها)")
        staff = sum(
            1 for t in self.db.open_tasks()
            if t.delegated and t.board == "today" and (t.due_date or today) <= today
        )
        if staff:
            lines.append(f"\n👥 {staff} کار هم دست پرسنله.")
        lines += ["", "روی هر کار بزن تا انجامش بدی یا عقبش بندازی.", self.streak_line(now)]
        return "\n".join(lines), shown

    def next_steps(self, now: datetime) -> list[str]:
        """One next step per building, in the Mayor's priority order."""
        today = self.local(now).date().isoformat()
        open_ = [t for t in self.db.open_tasks() if t.board not in ("waiting", "pipeline")]
        lines = []
        order = [k for k in self.cfg.building_priority if k in BUILDINGS]
        order += [k for k in BUILDINGS if k not in order and k != "vault"]
        for key in order:
            queue = sorted(
                (t for t in open_ if t.building == key and not (t.board == "today" and (t.due_date or today) <= today)),
                key=lambda t: (t.mission_id is None, t.step_order or 0, t.due_date or "9999", t.id),
            )
            if queue:
                t, b = queue[0], BUILDINGS[key]
                when = f" ({t.due_date})" if t.due_date else ""
                lines.append(f"{len(lines) + 1}. {b.emoji} {b.name}: {t.title}{when}")
        return lines

    def buildings_view(self, now: datetime) -> str:
        steps = self.next_steps(now)
        if not steps:
            return "🏗 همه ساختمون‌ها فعلاً قدم بعدی ندارن. یه کار جدید بنویس."
        text = "🏗 ساختمون‌های منتظر قدم بعدی (به ترتیب اولویت)\n\n" + "\n".join(steps)
        if any(t.kind == "publish" for t in self.db.open_tasks()):
            text += "\n\n" + self.buffer_line(now)
        return text

    def city_state(self, now: datetime) -> dict:
        """Everything the Mini App draws: the buildings, today's work, the streak."""
        today = self.local(now).date().isoformat()
        floors = self.floors()
        built = self.db.floors_by_building()
        open_ = self.db.open_tasks()

        def task_json(t: Task) -> dict:
            return {
                "id": t.id, "title": t.title, "building": t.building, "board": t.board,
                "due_date": t.due_date, "due_time": t.due_time,
                "overdue": bool(t.due_date and t.due_date < today and t.board == "today"),
                "staff": bool(t.delegated), "script": bool(t.notes), "high": t.priority == "high",
                "kind": t.kind, "brand": t.brand,
            }

        order = [k for k in self.cfg.building_priority if k in BUILDINGS]
        order += [k for k in BUILDINGS if k not in order]
        lit = self.lit_buildings(now)
        buildings = []
        for key in order:
            b = BUILDINGS[key]
            mine = [t for t in open_ if t.building == key and t.board != "pipeline"]
            mine.sort(key=lambda t: (t.board == "waiting", t.mission_id is None, t.step_order or 0,
                                     t.due_date or "9999", t.id))
            buildings.append({
                "key": key, "name": b.name, "emoji": b.emoji, "floors": floors.get(key, 0),
                "built": built.get(key, 0), "base": b.base, "district": b.district,
                "bridges": [k for k in b.bridges if k in BUILDINGS],
                "open": len(mine), "overdue": sum(1 for t in mine if task_json(t)["overdue"]),
                "priority": key in self.cfg.building_priority,
                "custom": key not in DEFAULT_BUILDINGS, "lit": key in lit,
                "tasks": [task_json(t) for t in mine[:12]],
            })
        has_content = any(t.kind == "publish" for t in open_)
        return {
            "today": [task_json(t) for t in self.today_tasks(now)],
            "buildings": buildings,
            "districts": [{"key": k, "name": n, "emoji": e, "about": a} for k, (n, e, a) in DISTRICTS.items()],
            "lights": {"lit": len([k for k in BUILDINGS if k != "vault" and k in lit]),
                       "total": len([k for k in BUILDINGS if k != "vault"])},
            "media": self.media_state(now),
            "built": sum(built.get(k, 0) for k in BUILDINGS),
            "streak": self.streak(now),
            "streak_line": self.streak_line(now),
            "buffer": self.content_buffer(now) if has_content else None,
            "buffer_line": self.buffer_line(now) if has_content else None,
            "date": today,
        }

    def all_view(self, now: datetime) -> str:
        today = self.local(now).date().isoformat()
        boards = {"today": "📌 روزانه", "waiting": "⏳ در انتظار", "construction": "🏗 در حال ساخت"}
        lines = []
        open_ = self.db.open_tasks()
        for board, title in boards.items():
            items = [t for t in open_ if t.board == board]
            if not items:
                continue
            lines.append(f"\n{title}")
            for t in items:
                mark = "⏰ " if t.due_date and t.due_date < today else ""
                when = f" ({t.due_date[5:]})" if t.due_date and t.due_date > today else ""
                staff = " 👥" if t.delegated else ""
                note = " 📝" if t.notes else ""
                lines.append(f"{mark}#{t.id} {BUILDINGS[t.building].emoji} {t.title}{when}{staff}{note}")
        return "\n".join(lines).strip() or "هیچ کار بازی نداری 🎉"
