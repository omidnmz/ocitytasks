"""The weekly marketing plan, read from the fixed template the Mayor pastes from his marketing chats.

    #برند: کراکس
    #هدف: ...
    [استوری]
    2026-11-14 | نظرسنجی رنگ | ...
    [ریلز]
    2026-11-16 | راهنمای سایز | ریل | آماده‌سازی
    سناریو:
    هوک: ...
    شات‌لیست: ...
    [کارها]
    پیام به ۵ مربی | محصول
    #معیار نتیجه: تعداد دایرکت

Several plans can come in one message: each starts at its own «#برند:» line.
A reel's full script goes on the lines under its row, after «سناریو:», until the next row or section.
The older one-line form (a short script in the 4th cell, prep in the 5th) still reads.
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
REEL_QUOTA = {"retail": "۳ تا ۵", "smarto": "۲", "fitness": "۲"}  # reels a week per page

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
    full: bool = False  # the script came as its own block under the row


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


def _is_section(line: str) -> bool:
    bare = re.sub(r"[\[\]【】()#*:：]", "", line).replace("\u200cها", "").replace("\u200c", " ").strip()
    return ("|" not in line and bare in SECTIONS) or bool(re.match(r"^#\s*[^:：]+[:：]", line))


def _add_script_line(reel: Reel, line: str) -> None:
    """A line under a reel's row: the start of its full script, or the next line of it."""
    head = re.match(r"^\**\s*(سناریو(?:\s*کامل)?|script)\s*\**\s*[:：]\s*(.*)$", line, re.I)
    if not reel.full:
        # the script comes as its own block, so the row's 4th cell was the prep, not a short script
        if reel.script and not reel.prep:
            reel.prep = reel.script
        reel.script, reel.full = "", True
        if head:
            line = head[2].strip()
            if not line:
                return
    reel.script = f"{reel.script}\n{line}" if reel.script else line


def parse(text: str) -> list[Plan]:
    plans: list[Plan] = []
    plan: Plan | None = None
    section = None
    for raw, original in zip(text.translate(PERSIAN_DIGITS).splitlines(), text.splitlines()):
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
        if (plan is not None and section == "reels" and plan.reels and not _is_section(line)
                and ("|" not in line or _day(line.strip("|").split("|")[0]) is None)):
            _add_script_line(plan.reels[-1], original.strip())  # the script keeps its own digits
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


def prompt(brand: str, start: date, end: date, last_metric: str = "", reels: str = "۲ تا ۵") -> str:
    """The text the Mayor pastes into a brand's marketing chat."""
    ask_result = (f"\nنتیجه هفته قبل ({last_metric}) این بود: ...  (قبل از فرستادن، عددش رو بنویس)\n"
                  if last_metric else "")
    return (
        f"برنامه مارکتینگ هفته آینده (از {start.isoformat()} تا {end.isoformat()}) رو برای برند «{brand}» بده.{ask_result}\n"
        "فقط با همین قالب جواب بده، بدون هیچ توضیح اضافه. تاریخ‌ها میلادی و به شکل YYYY-MM-DD باشن.\n\n"
        f"#برند: {brand}\n#هدف: (یه جمله)\n#مخاطب: (یه جمله)\n#پیام اصلی: (یه جمله)\n\n"
        "[استوری]\n(۷ خط، یکی برای هر روز)\nتاریخ | موضوع استوری | توضیح کوتاه یا ایده\n\n"
        f"[ریلز]\n({reels} ریل؛ برای هر کدوم یه خط جدول و زیرش سناریوی کامل)\n"
        "تاریخ انتشار | عنوان | فرمت (ریل/پست/کاروسل) | آماده‌سازی لازم\n"
        "سناریو:\n"
        "🎬 هوک (۰ تا ۳ ثانیه): جمله یا تصویر دقیق شروع\n"
        "🎥 شات‌لیست: شات به شات، با زاویه‌ی دوربین و مدت هر شات\n"
        "🗣 متن کامل گفتار یا ویس‌اور، کلمه به کلمه\n"
        "📝 متن‌های روی تصویر\n"
        "🎵 حس موزیک یا صدا\n"
        "✅ پایان و دعوت به اقدام\n"
        "✍️ کپشن کامل با هشتگ‌ها\n"
        "(سناریو رو کامل و چندخطی بنویس، خلاصه نکن. بعد خط ریل بعدی.)\n\n"
        "[کارها]\n(کارهای غیرمحتوایی، اگه هست)\nعنوان کار | کدوم بخش (محصول/سایت/پرسنل)\n\n"
        "#معیار نتیجه: (عددی که آخر هفته باید چک کنم)"
    )
