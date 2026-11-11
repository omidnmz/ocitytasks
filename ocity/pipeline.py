"""The weekly marketing plan, read from the fixed template the Mayor pastes from his marketing chats.

    #برند: کراکس
    #هدف: ...
    [استوری]
    2026-11-14 | نظرسنجی رنگ | ...
    [ریلز]
    2026-11-16 | راهنمای سایز | ریل | سناریو | آماده‌سازی
    [کارها]
    پیام به ۵ مربی | محصول
    #معیار نتیجه: تعداد دایرکت

Several plans can come in one message: each starts at its own «#برند:» line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# a brand's name in the plan -> the building it belongs to
BRANDS: dict[str, tuple[str, ...]] = {
    "retail": ("کراکس", "crocs", "کیش"),
    "smarto": ("اسمارتو", "smarto", "اسمارت او"),
    "fitness": ("فیتنس", "fitness"),
    "technician": ("تکنسین",),
    "retailos": ("retail os", "ریتیل"),
}
MARKETING_BRANDS = ("retail", "smarto", "fitness")  # the pages that get a weekly plan

FIELDS = {"هدف": "goal", "مخاطب": "audience", "پیام اصلی": "message", "پیام": "message",
          "معیار نتیجه": "metric", "معیار": "metric", "نتیجه هفته قبل": "last_result"}
SECTIONS = {"استوری": "stories", "story": "stories", "ریلز": "reels", "ریل": "reels", "reels": "reels",
            "محتوا": "reels", "کارها": "tasks", "کار": "tasks", "tasks": "tasks"}


@dataclass
class Story:
    day: date
    topic: str
    note: str = ""


@dataclass
class Reel:
    day: date
    title: str
    format: str = "ریل"
    script: str = ""
    prep: str = ""


@dataclass
class Action:
    title: str
    part: str = ""


@dataclass
class Plan:
    brand: str  # the name as written
    building: str | None
    goal: str = ""
    audience: str = ""
    message: str = ""
    metric: str = ""
    last_result: str = ""
    stories: list[Story] = field(default_factory=list)
    reels: list[Reel] = field(default_factory=list)
    tasks: list[Action] = field(default_factory=list)


def brand_building(name: str) -> str | None:
    low = name.lower().replace("‌", " ").strip()
    for key, words in BRANDS.items():
        if any(w in low for w in words):
            return key
    return None


def _day(cell: str) -> date | None:
    try:
        return date.fromisoformat(cell.strip().replace("/", "-"))
    except ValueError:
        return None


def is_marketing_plan(text: str) -> bool:
    return bool(re.search(r"^\s*#\s*برند\s*[:：]", text, re.M))


def parse(text: str) -> list[Plan]:
    plans: list[Plan] = []
    plan: Plan | None = None
    section = None
    for raw in text.translate(PERSIAN_DIGITS).splitlines():
        line = raw.strip().strip("*").strip()
        if not line:
            continue
        if m := re.match(r"^#\s*([^:：]+?)\s*[:：]\s*(.*)$", line):
            key, value = m[1].strip(), m[2].strip()
            if key == "برند":
                plan = Plan(value, brand_building(value))
                plans.append(plan)
                section = None
            elif plan and key in FIELDS:
                setattr(plan, FIELDS[key], value)
            continue
        bare = re.sub(r"[\[\]【】()#*:：]", "", line).replace("\u200cها", "").replace("\u200c", " ").strip()
        if "|" not in line and bare in SECTIONS:
            section = SECTIONS[bare]
            continue
        if plan is None or section is None or "|" not in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if section == "stories":
            day = _day(cells[0])
            if day and len(cells) > 1 and cells[1]:
                plan.stories.append(Story(day, cells[1], cells[2] if len(cells) > 2 else ""))
        elif section == "reels":
            day = _day(cells[0])
            if day and len(cells) > 1 and cells[1]:
                plan.reels.append(Reel(day, cells[1], cells[2] if len(cells) > 2 and cells[2] else "ریل",
                                       cells[3] if len(cells) > 3 else "", cells[4] if len(cells) > 4 else ""))
        elif section == "tasks" and cells[0] and not cells[0].startswith("عنوان"):
            plan.tasks.append(Action(cells[0], cells[1] if len(cells) > 1 else ""))
    return plans


def prompt(brand: str, start: date, end: date, last_metric: str = "") -> str:
    """The text the Mayor pastes into a brand's marketing chat."""
    ask_result = (f"\nنتیجه هفته قبل ({last_metric}) این بود: ...  (قبل از فرستادن، عددش رو بنویس)\n"
                  if last_metric else "")
    return (
        f"برنامه مارکتینگ هفته آینده (از {start.isoformat()} تا {end.isoformat()}) رو برای برند «{brand}» بده.{ask_result}\n"
        "فقط با همین قالب جواب بده، بدون هیچ توضیح اضافه. تاریخ‌ها میلادی و به شکل YYYY-MM-DD باشن.\n\n"
        f"#برند: {brand}\n#هدف: (یه جمله)\n#مخاطب: (یه جمله)\n#پیام اصلی: (یه جمله)\n\n"
        "[استوری]\n(۷ خط، یکی برای هر روز)\nتاریخ | موضوع استوری | توضیح کوتاه یا ایده\n\n"
        "[ریلز]\n(۲ تا ۵ خط)\nتاریخ انتشار | عنوان | فرمت (ریل/پست/کاروسل) | سناریو کوتاه و شات‌لیست | آماده‌سازی لازم\n\n"
        "[کارها]\n(کارهای غیرمحتوایی، اگه هست)\nعنوان کار | کدوم بخش (محصول/سایت/پرسنل)\n\n"
        "#معیار نتیجه: (عددی که آخر هفته باید چک کنم)"
    )
