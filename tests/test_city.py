from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ocity import learning
from ocity.ai import MessagePlan, TaskIn
from ocity.city import CityHall
from ocity.config import Config
from ocity.db import DB

TZ = ZoneInfo("Asia/Tehran")


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=TZ)


@pytest.fixture
def city():
    cfg = Config(quiet_windows=[(time(18, 30), time(20, 0))])
    return CityHall(DB(":memory:"), cfg)


def test_task_with_time_is_nudged_then_not_again(city):
    task = city.add("call Sea Club", "retail", now=at(2, 9), due_time="12:00")
    assert not [m for m in city.tick(at(2, 11, 59)) if m.task_id == task.id]
    msgs = [m for m in city.tick(at(2, 12, 0)) if m.task_id == task.id]
    assert len(msgs) == 1
    assert not [m for m in city.tick(at(2, 12, 1)) if m.task_id == task.id]


def test_high_priority_gets_one_renudge(city):
    task = city.add("pay supplier", "retail", now=at(2, 9), due_time="12:00", priority="high")
    city.tick(at(2, 12))
    assert [m for m in city.tick(at(2, 14, 1)) if m.task_id == task.id]
    assert not [m for m in city.tick(at(2, 16, 2)) if m.task_id == task.id]


def test_quiet_window_delays_nudge(city):
    task = city.add("post office", "retail", now=at(2, 9), due_time="19:00")
    nudge = datetime.fromisoformat(city.db.get_task(task.id).next_nudge_at)
    assert city.local(nudge).time() == time(20, 0)


def test_snooze_counts_postpone_and_tomorrow_moves_due(city):
    task = city.add("edit reel", "media", now=at(2, 9))
    city.snooze(task.id, "30", at(2, 13))
    city.snooze(task.id, "tomorrow", at(2, 14))
    t = city.db.get_task(task.id)
    assert t.postpone_count == 2
    assert t.due_date == "2026-11-03"


def test_morning_and_evening_fire_once_and_evening_carries_over(city):
    city.add("woocommerce update", "retail", now=at(2, 9))
    msgs = city.tick(at(2, 10, 30))
    assert any("صبح بخیر" in m.text for m in msgs)
    assert not any("صبح بخیر" in m.text for m in city.tick(at(2, 10, 31)))
    msgs = city.tick(at(2, 23, 0))
    assert any("جمع‌بندی" in m.text for m in msgs)
    t = city.db.open_tasks()[0]
    assert t.due_date == "2026-11-03" and t.postpone_count == 1


def test_learning_moves_nudge_to_usual_time(city):
    for day in (2, 3, 4):
        t = city.add("woocommerce update", "retail", now=at(day, 9), kind="woocommerce")
        city.done(t.id, at(day, 12, 40))
    profile = city.profile()
    assert profile["woocommerce"].usual_time == time(12, 40)
    new = city.add("woocommerce update", "retail", now=at(5, 9), kind="woocommerce")
    nudge = datetime.fromisoformat(city.db.get_task(new.id).next_nudge_at)
    assert city.local(nudge) == at(5, 12, 40)
    assert "12:40" in learning.describe(profile)


def test_seed_starts_content_sprint_and_followups(city):
    city.seed(at(2, 9))
    city.seed(at(2, 9))  # idempotent
    missions = city.db.active_missions(at(2, 9))
    assert len(missions) == 1
    msgs = city.tick(at(2, 12, 30))
    sprint = [m for m in msgs if "اسپرینت" in m.text]
    assert len(sprint) == 1 and "/plan" in sprint[0].text
    # a restart late in the day does not replay the missed slots
    assert not [m for m in city.tick(at(2, 12, 31)) if "اسپرینت" in m.text]


def test_mission_closes_when_all_steps_done(city):
    mission_id = city.start_content_sprint(at(2, 9))
    for step in city.db.mission_steps(mission_id):
        city.done(step.id, at(2, 10))
    msgs = city.tick(at(2, 10, 1))
    assert any("تموم شد" in m.text for m in msgs)
    assert not city.db.active_missions(at(2, 10, 2))


def test_checkin_lists_untimed_today_tasks(city):
    a = city.add("refill shelves", "retail", now=at(2, 9))
    msgs = city.tick(at(2, 14, 0))
    checkin = [m for m in msgs if m.done_buttons]
    assert checkin and checkin[0].done_buttons[0].id == a.id


class FakeAI:
    def understand(self, message, context, now_local):
        assert "Open tasks" in context
        return MessagePlan(
            reply="ثبت شد",
            add_tasks=[TaskIn(title="زنگ به Sea Club", building="retail", board="today", priority="high",
                              kind="follow_up", due_date="2026-11-05", due_time="18:00",
                              estimate_min=None, delegated=False)],
            complete_task_ids=[1],
            add_rules=["جمعه‌ها کار سایت نمی‌کنم"],
        )


def test_handle_message_applies_plan(city):
    first = city.add("old task", "cityhall", now=at(2, 9))
    city.ai = FakeAI()
    result = city.handle_message("...", at(2, 10))
    assert city.db.get_task(first.id).status == "done"
    assert result.added[0].due_time == "18:00"
    assert city.db.rules() == ["جمعه‌ها کار سایت نمی‌کنم"]


def test_without_ai_message_becomes_cityhall_task(city):
    result = city.handle_message("buy GoPro mic", at(2, 10))
    assert result.added[0].building == "cityhall"
    assert result.added[0].due_date == "2026-11-02"


def test_city_view_shows_floors_and_scaffolding(city):
    t = city.add("x", "media", now=at(2, 9))
    city.done(t.id, at(2, 10))
    city.add("late", "retail", now=at(1, 9), due_date="2026-11-01")
    view = city.city_view(at(2, 11))
    assert "Media Hub: ▮ 1 طبقه" in view
    assert "داربست" in view


def test_plan_table_imports_without_ai(city):
    text = """| تاریخ | بیزنس | عنوان | فرمت | آماده‌سازی |
|---|---|---|---|---|
| ۲۰۲۶-۱۱-۰۵ | کراکس | راهنمای سایز | ریل | کلاسیک مشکی و سفید |
2026-11-06 | SmartO | معرفی پنل | موشن |
"""
    result = city.import_content_plan(text, at(2, 10))
    titles = [t.title for t in result.added]
    assert len(result.added) == 3
    assert any("راهنمای سایز" in t and "انتشار" in t for t in titles)
    prep = [t for t in result.added if t.kind == "content_prep"][0]
    assert prep.due_date == "2026-11-03" and prep.delegated


def test_unreadable_plan_without_ai_explains_format(city):
    result = city.import_content_plan("this week film the clogs", at(2, 10))
    assert not result.added and "|" in result.reply


HANDOFF = """# Media Hub handoff, week 1
### 2026-11-05 | کراکس | راهنمای سایز | ریل | کلاسیک مشکی و سفید
شات ۱: دیوار رنگ‌ها
شات ۲: پوشیدن سایز M9
کپشن: سایزت رو با ما پیدا کن

### 2026-11-06 | SmartO | معرفی پنل | موشن |
"""


def test_handoff_scripts_become_filming_tasks_and_arrive_on_the_day(city):
    result = city.import_content_plan(HANDOFF, at(2, 10))
    filming = [t for t in result.added if t.kind == "filming"]
    assert len(filming) == 1
    assert filming[0].due_date == "2026-11-04"
    assert "شات ۲" in filming[0].notes and "کپشن" in filming[0].notes
    smarto = [t for t in result.added if "معرفی پنل" in t.title]
    assert len(smarto) == 1 and smarto[0].notes is None
    # nothing on the 3rd, the script arrives with the morning brief on the 4th
    assert not [m for m in city.tick(at(3, 10, 30)) if m.text.startswith("📝")]
    scripts = [m for m in city.tick(at(4, 10, 30)) if m.text.startswith("📝")]
    assert len(scripts) == 1 and scripts[0].task_id == filming[0].id


def test_old_database_gets_notes_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, building TEXT NOT NULL, "
                 "board TEXT NOT NULL DEFAULT 'today', status TEXT NOT NULL DEFAULT 'open', priority TEXT NOT NULL DEFAULT 'normal', "
                 "kind TEXT, due_date TEXT, due_time TEXT, next_nudge_at TEXT, nudge_count INTEGER NOT NULL DEFAULT 0, "
                 "postpone_count INTEGER NOT NULL DEFAULT 0, estimate_min INTEGER, actual_min INTEGER, "
                 "delegated INTEGER NOT NULL DEFAULT 0, mission_id INTEGER, step_order INTEGER, created_at TEXT NOT NULL, done_at TEXT)")
    conn.execute("INSERT INTO tasks(title, building, created_at) VALUES('old', 'retail', '2026-11-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    db = DB(str(path))
    assert db.get_task(1).notes is None
    assert db.add_task("new", "media", notes="script").notes == "script"
