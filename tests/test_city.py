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


def test_seed_gives_each_brand_page_a_marketing_plan_task(city):
    city.seed(at(2, 9))
    city.seed(at(2, 9))  # idempotent
    plans = [t for t in city.db.open_tasks() if t.kind == "mkt_plan"]
    assert sorted(t.brand for t in plans) == ["fitness", "retail", "smarto"]
    assert all(t.building == "marketing" for t in plans)
    assert not city.db.active_missions(at(2, 9))


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
    # filmed on Tuesday the 3rd (a production day), staff prep the day before
    assert prep.due_date == "2026-11-02" and prep.delegated


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
    assert filming[0].due_date == "2026-11-03"  # Tuesday, a production day
    assert "شات ۲" in filming[0].notes and "کپشن" in filming[0].notes
    smarto = [t for t in result.added if "معرفی پنل" in t.title]
    assert len(smarto) == 1 and smarto[0].notes is None
    # nothing before the day; on the 3rd City Hall offers the start button, scripts come one by one
    assert not [m for m in city.tick(at(2, 23, 59)) if m.start_kind == "filming"]
    calls = [m for m in city.tick(at(3, 10, 30)) if m.start_kind == "filming"]
    assert len(calls) == 1
    assert city.stage_queue("filming", at(3, 10, 31))[0].id == filming[0].id


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


def test_sending_a_plan_again_does_not_duplicate(city):
    half = HANDOFF.split("### 2026-11-06")[0]
    first = city.import_content_plan(half, at(2, 10))
    assert len(first.added) == 3  # publish, filming, prep
    second = city.import_content_plan(HANDOFF, at(2, 11))
    assert len(second.added) == 1  # only the SmartO piece is new
    media = [t for t in city.db.open_tasks() if t.building == "media" and t.mission_id is None]
    assert len(media) == 4


def test_drop_removes_task_from_open_list(city):
    t = city.add("junk", "cityhall", now=at(2, 9))
    city.drop(t.id, at(2, 10))
    assert t.id not in [x.id for x in city.db.open_tasks()]


def test_digest_shows_today_and_one_next_step_per_building(city):
    city.seed(at(2, 9))
    city.import_content_plan(HANDOFF, at(2, 9))
    digest = city.tasks_view(at(2, 10))
    media_line = [l for l in digest.splitlines() if "🎬 Media Hub:" in l]
    assert len(media_line) == 1
    full = city.all_view(at(2, 10))
    assert "معرفی پنل" in full


def test_media_hub_comes_first_in_the_priority_list(city):
    city.add("buy domain", "smarto", "construction", now=at(2, 9))
    city.add("film size guide", "media", "construction", now=at(2, 9))
    digest = city.tasks_view(at(2, 10))
    assert "1. 🎬 Media Hub" in digest and "2. 🤖 SmartO" in digest


def test_marketing_day_asks_for_each_brand_plan_until_it_arrives(city):
    # 2026-11-06 is a Friday, the marketing day
    msgs = city.tick(at(6, 10, 30))
    assert any("روز برنامه‌ریزی مارکتینگه" in m.text for m in msgs)
    prompts = [m for m in msgs if m.text.startswith("برنامه مارکتینگ هفته آینده")]
    assert len(prompts) == 3 and "#برند:" in prompts[0].text
    assert any("هنوز برنامه مارکتینگ" in m.text for m in city.tick(at(7, 10, 30)))
    for brand in ("کراکس", "SmartO", "Fitness OS"):
        city.import_marketing_plan(f"#برند: {brand}\n[استوری]\n2026-11-08 | سلام | ", at(7, 11))
    assert not any("مارکتینگ" in m.text for m in city.tick(at(8, 10, 30)))


def test_production_day_opens_with_the_batch_and_buffer_counts_filmed_pieces(city):
    city.import_content_plan(HANDOFF, at(2, 9))
    msgs = city.tick(at(3, 10, 30))  # Tuesday, a production day
    assert len([m for m in msgs if m.start_kind == "filming"]) == 1
    filming = [t for t in city.db.open_tasks() if t.kind == "filming"][0]
    city.done(filming.id, at(3, 12))
    assert city.content_buffer(at(3, 13)) == 1


# --- the assistant: capture, reminders, keeping the day ----------------------

def test_capture_guesses_building_and_reads_time(city):
    r = city.handle_message("فردا ساعت ۶ عصر به مشتری کراکس زنگ بزنم", at(11, 16))
    t = r.added[0]
    assert (t.building, t.due_date, t.due_time) == ("retail", "2026-11-12", "18:00")
    assert not r.ask_building and not r.ask_when
    assert "فردا ساعت 18:00" in r.reply


def test_capture_asks_building_and_time_when_unclear(city):
    r = city.handle_message("بیمه ماشین", at(11, 16))
    assert r.ask_building and r.ask_when
    t = city.set_building(r.added[0].id, "personal")
    assert t.building == "personal"


def test_idea_goes_to_the_vault_without_reminder(city):
    r = city.handle_message("ایده: ویدیوی پشت صحنه کلینیک", at(11, 16))
    t = r.added[0]
    assert (t.building, t.board, t.next_nudge_at) == ("vault", "construction", None)
    assert not r.ask_when


def test_reminder_buttons_and_typed_time(city):
    t = city.handle_message("تدوین ریل سایز", at(11, 16)).added[0]
    assert city.remind(t.id, "evening", at(11, 16)) == "امروز ساعت 18:00"
    assert city.local(datetime.fromisoformat(city.db.get_task(t.id).next_nudge_at)).time() == time(18, 0)
    assert city.remind_from_text(t.id, "پنجشنبه ساعت ۵", at(11, 16)) == "فردا ساعت 17:00"
    assert city.remind_from_text(t.id, "باشه", at(11, 16)) is None
    city.remind(t.id, "none", at(11, 16))
    task = city.db.get_task(t.id)
    assert (task.board, task.next_nudge_at) == ("construction", None)


def test_stuck_task_is_flagged_and_can_be_split(city):
    t = city.add("لیست کلینیک‌ها", "technician", now=at(11, 9), due_time="12:00")
    for _ in range(3):
        city.snooze(t.id, "30", at(11, 12))
    msgs = [m for m in city.tick(at(11, 12, 31)) if m.task_id == t.id]
    assert msgs and msgs[0].stuck and "عقب افتاده" in msgs[0].text
    step = city.split(t.id, "فقط ۵ تا کلینیک رو اسم ببرم", at(11, 13))
    assert step.building == "technician" and step.next_nudge_at
    assert city.db.get_task(t.id).board == "construction"


def test_streak_counts_days_in_a_row(city):
    for day in (8, 9, 10):
        t = city.add("کار", "media", now=at(day, 9))
        city.done(t.id, at(day, 12))
    assert city.streak(at(11, 9)) == 3
    assert "نشکنه" in city.streak_line(at(11, 9))
    t = city.add("کار", "media", now=at(11, 9))
    city.done(t.id, at(11, 12))
    assert city.streak(at(11, 13)) == 4


def test_away_ping_and_rescue_nudge(city):
    city.add("ضبط ریل", "media", now=at(11, 9))
    city.tick(at(11, 10, 30))  # morning brief
    msgs = city.tick(at(11, 13, 30))
    assert any("سر نزدی" in m.text for m in msgs)
    msgs = city.tick(at(11, 21, 0))
    assert any("هیچ طبقه‌ای" in m.text for m in msgs)


def test_no_away_ping_when_mayor_showed_up(city):
    city.add("ضبط ریل", "media", now=at(11, 9))
    city.tick(at(11, 10, 30))
    city.seen(at(11, 11))
    t = city.add("کار", "media", now=at(11, 11))
    city.done(t.id, at(11, 12))
    assert not any("سر نزدی" in m.text for m in city.tick(at(11, 13, 30)))
    assert not any("هیچ طبقه‌ای" in m.text for m in city.tick(at(11, 21, 0)))


def test_evening_lists_carried_tasks_with_buttons(city):
    t = city.add("آپدیت سایت", "retail", now=at(11, 9))
    msgs = [m for m in city.tick(at(11, 23, 0)) if "جمع‌بندی" in m.text]
    assert msgs and [x.id for x in msgs[0].done_buttons] == [t.id]


def test_weekly_review_on_review_day(city):
    t = city.add("ریل", "media", now=at(10, 9))
    city.done(t.id, at(10, 12))
    city.add("دامین", "smarto", "construction", now=at(10, 9))
    msgs = city.tick(at(13, 10, 30))  # friday
    review = [m for m in msgs if "مرور هفته" in m.text]
    assert review and "دست نخوردن" in review[0].text


def test_today_view_is_short_and_has_a_button_per_task(city):
    old = city.add("قدیمی", "retail", now=at(10, 9))
    city.add("ضبط ریل", "media", now=at(11, 9), due_time="12:00")
    city.add("فردا", "smarto", now=at(11, 9), due_date="2026-11-12")
    city.add("پرسنل", "retail", now=at(11, 9), delegated=True)
    text, items = city.today_view(at(11, 10))
    assert [t.title for t in items] == ["قدیمی", "ضبط ریل"]
    assert "فردا" not in text and "1 کار هم دست پرسنله" in text
    assert items[0].id == old.id


def test_buildings_view_lists_next_steps_in_priority_order(city):
    city.add("دامین", "smarto", "construction", now=at(11, 9))
    city.add("ریل هفته بعد", "media", now=at(11, 9), due_date="2026-11-14")
    lines = city.buildings_view(at(11, 10)).splitlines()
    assert lines[2].startswith("1. 🎬") and lines[3].startswith("2. 🤖")


def test_empty_today_suggests_next_step(city):
    city.add("دامین", "smarto", "construction", now=at(11, 9))
    text, items = city.today_view(at(11, 10))
    assert not items and "دامین" in text


def test_buildings_can_be_renamed_prioritised_and_added():
    from ocity.buildings import BUILDINGS
    db = DB(":memory:")
    city = CityHall(db, Config())
    city.update_building("media", name="  مدیا  ", emoji="📹", priority=False)
    assert BUILDINGS["media"].name == "مدیا" and BUILDINGS["media"].emoji == "📹"
    assert "media" not in city.cfg.building_priority
    key = city.add_building("آشپزخونه", "🍳")
    assert key == "c1" and BUILDINGS[key].name == "آشپزخونه"
    assert city.add_building("دومی") == "c2"
    # a fresh city hall on the same database remembers the Mayor's choices
    again = CityHall(db, Config())
    assert BUILDINGS["media"].name == "مدیا" and "media" not in again.cfg.building_priority and "c2" in BUILDINGS
    # and another database starts from the defaults
    CityHall(DB(":memory:"), Config())
    assert BUILDINGS["media"].name != "مدیا" and "c1" not in BUILDINGS


def test_running_businesses_start_tall_and_tasks_move_to_their_product():
    db = DB(":memory:")
    city = CityHall(db, Config())
    floors = city.floors()
    assert floors["retail"] == 10 and floors["technician"] == 4 and floors["tradebot"] == 3 and floors["smarto"] == 7
    assert floors["fitness"] == 5 and floors["media"] == 0
    # an old Retail OS task living in the Crocs building moves to Retail OS once
    db.set_state("districts_v1", "")
    tid = db.add_task("ساخت ربات موجودی تلگرام برای شعبه‌ها", "retail", board="construction").id
    other = db.add_task("چک موجودی شعبه رشت", "retail", board="today").id
    CityHall(db, Config())
    assert db.get_task(tid).building == "retailos" and db.get_task(other).building == "retail"
    state = city.city_state(datetime(2026, 11, 11, 10, tzinfo=ZoneInfo("UTC")))
    by = {b["key"]: b for b in state["buildings"]}
    assert by["retailos"]["district"] == "tech" and "retail" in by["retailos"]["bridges"]
    assert {d["key"] for d in state["districts"]} >= {"downtown", "tech", "support", "center", "personal", "future"}
    city.update_building("tradebot", district="tech")
    assert city.city_state(datetime(2026, 11, 11, 10, tzinfo=ZoneInfo("UTC")))["buildings"][0]  # still builds
    from ocity.buildings import BUILDINGS
    assert BUILDINGS["tradebot"].district == "tech" and BUILDINGS["tradebot"].base == 3


PLAN = """#برند: کراکس
#هدف: فروش بیشتر کلاسیک
#معیار نتیجه: تعداد دایرکت

[استوری]
2026-11-14 | نظرسنجی رنگ | مشکی یا سفید
2026-11-15 | پشت صحنه انبار |

[ریلز]
2026-11-17 | راهنمای سایز | ریل | شات ۱ دیوار رنگ | کلاسیک مشکی
2026-11-19 | سه رنگ پرفروش | ریل | شات ۱ |

[کارها]
عنوان کار | بخش
آپدیت عکس‌های سایت | سایت
چیدن قفسه رنگ‌ها | پرسنل

#برند: Fitness OS
[استوری]
2026-11-14 | حرکت روز |
[کارها]
پیام به ۵ مربی | محصول
"""


def test_marketing_plan_splits_into_stories_reels_and_tasks():
    city = CityHall(DB(":memory:"), Config())
    city.seed(at(12, 9))
    r = city.import_marketing_plan(PLAN, at(12, 22))
    kinds = [t.kind for t in r.added]
    assert kinds.count("story") == 3 and kinds.count("filming") == 2 and kinds.count("content_prep") == 1
    # the marketing tower's orders for these two brands are done and gave it floors
    assert {t.brand for t in city.db.open_tasks() if t.kind == "mkt_plan"} == {"smarto"}
    assert city.floors()["marketing"] == 2
    # reels are filmed on Sunday the 15th (a day left for editing), the card waits on the line
    film = [t for t in r.added if t.kind == "filming"]
    assert {t.due_date for t in film} == {"2026-11-15"}
    card = city.db.get_task(film[0].link_id)
    assert card.board == "pipeline" and card.kind == "publish" and card.brand == "retail"
    assert card.id not in [t.id for t in city.today_tasks(at(17, 10))]
    # other work goes to the brand's building; staff work is marked for staff
    shelf = [t for t in r.added if "قفسه" in t.title][0]
    coach = [t for t in r.added if "مربی" in t.title][0]
    assert shelf.building == "retail" and shelf.delegated and coach.building == "fitness"
    # City Hall has to learn when filming, editing and publishing happen
    assert r.ask_times == ["filming", "editing", "publish"]
    city.set_kind_time("filming", "10:00")
    assert all(city.db.get_task(t.id).due_time == "10:00" for t in film)


def test_content_line_moves_from_filming_to_editing_to_publish():
    city = CityHall(DB(":memory:"), Config())
    city.set_kind_time("editing", "17:00")
    r = city.import_marketing_plan(PLAN, at(12, 22))
    film = sorted((t for t in r.added if t.kind == "filming"), key=lambda t: t.id)[0]
    done = city.done(film.id, at(15, 11))
    assert "تدوین" in done.next_step and "17:00" in done.next_step
    edit = [t for t in city.db.open_tasks() if t.kind == "editing"][0]
    assert edit.due_date == "2026-11-16" and edit.link_id == film.link_id  # Monday
    assert city.content_buffer(at(15, 12)) == 1 and city.buffers(at(15, 12)) == {"retail": 1}
    done = city.done(edit.id, at(16, 18))
    assert "انتشار" in done.next_step
    card = city.db.get_task(film.link_id)
    assert card.board == "today" and card.due_date == "2026-11-17"
    cards, stories = city.media_board(at(16, 18))
    assert [c["stage"] for c in cards] == ["publish", "filming"] and stories == []  # the stories were last week


def test_city_hall_calls_stories_filming_and_the_evening_before():
    city = CityHall(DB(":memory:"), Config())
    city.import_marketing_plan(PLAN, at(12, 22))
    msgs = city.city_hall_calls(at(14, 11, 0))
    stories = [m for m in msgs if "استوری‌های امروز" in m.text]
    assert len(stories) == 1 and len(stories[0].done_buttons) == 2
    assert city.city_hall_calls(at(14, 11, 1)) == []
    city.done(stories[0].done_buttons[0].id, at(14, 11, 30))
    again = [m for m in city.city_hall_calls(at(14, 13, 0)) if "استوری" in m.text]
    assert len(again) == 1 and len(again[0].done_buttons) == 1
    assert "retail" in city.lit_buildings(at(14, 12)) and "media" in city.lit_buildings(at(14, 12))
    eve = [m for m in city.city_hall_calls(at(14, 21, 0)) if "فردا Media Hub" in m.text]
    assert len(eve) == 1 and "فیلم‌برداری" in eve[0].text and "2 محتوا" in eve[0].text
    start = [m for m in city.city_hall_calls(at(15, 10, 31)) if m.start_kind == "filming"]
    assert len(start) == 1
    # a missed story does not pile onto tomorrow
    city.carry_over(at(14, 23))
    assert not [t for t in city.db.open_tasks() if t.kind == "story" and t.due_date == "2026-11-15" and "حرکت" in t.title]


def test_city_hall_grows_with_each_day_the_mayor_shows_up():
    city = CityHall(DB(":memory:"), Config())
    city.seen(at(12, 9)); city.seen(at(12, 15)); city.seen(at(13, 9))
    assert city.floors()["cityhall"] == 2 and "cityhall" in city.lit_buildings(at(13, 10))
