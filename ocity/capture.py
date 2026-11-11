"""Understanding a quick note without Claude: which building, and when.

«فردا ساعت ۶ عصر به مشتری کراکس زنگ بزنم» -> retail, tomorrow 18:00.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# building -> words that point to it (lowercase, checked as substrings)
KEYWORDS: dict[str, tuple[str, ...]] = {
    "vault": ("ایده",),
    "retailos": ("retail os", "ریتیل", "ربات موجودی", "بات موجودی", "شیت موجودی", "شیت محصولات", "گوگل شیت",
                 "google sheet"),
    "techos": ("technician os", "تکنسین او اس", "پلتفرم تکنسین", "اپ تکنسین"),
    "knowledge": ("دوره", "کورس", "پایگاه دانش", "خلاصه درس", "جلسه آموزشی"),
    "smarto": ("اسمارتو", "smarto", "اسمارت او", "دامین", "سرور تست"),
    "technician": ("کلینیک", "دندان", "تکنسین", "لابراتوار", "روکش", "ایمپلنت"),
    "fitness": ("فیتنس", "fitness", "باشگاه", "مینی اپ", "تستر"),
    "tradebot": ("ترید", "trade", "بورس", "کریپتو", "سیگنال", "بک تست", "بکتست"),
    "media": ("محتوا", "ریلز", "ریل", "استوری", "پست", "ضبط", "فیلم", "تدوین", "موشن",
              "اینستا", "کپشن", "سناریو", "ادیت", "مدیاهاب", "media"),
    "marketing": ("مارکتینگ", "تبلیغ", "کمپین", "بازاریابی", "اینفلوئنسر", "ads"),
    "retail": ("کراکس", "crocs", "دمپایی", "ووکامرس", "سایت", "انبار", "موجودی", "فروشگاه",
               "مغازه", "مشتری", "سفارش", "تامین", "پرداخت", "کیش"),
    "personal": ("خونه", "خانه", "خانواده", "دکتر", "قبض", "بانک", "خرید شخصی", "تولد", "ورزش"),
}

WEEKDAYS_FA = {  # python weekday numbers; longest names first so پنجشنبه is not read as شنبه
    "پنجشنبه": 3, "پنج شنبه": 3, "سه شنبه": 1, "سهشنبه": 1,
    "چهارشنبه": 2, "یکشنبه": 6, "یک شنبه": 6, "دوشنبه": 0, "جمعه": 4, "شنبه": 5,
}


WEEKDAY_NAMES = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def describe_when(day: date | None, at: time | None, today: date) -> str:
    if day is None:
        return "بدون یادآوری"
    if day == today:
        name = "امروز"
    elif day == today + timedelta(days=1):
        name = "فردا"
    elif day - today < timedelta(days=7):
        name = WEEKDAY_NAMES[day.weekday()]
    else:
        name = f"{WEEKDAY_NAMES[day.weekday()]} {day.isoformat()}"
    return f"{name} ساعت {at:%H:%M}" if at else name


@dataclass
class When:
    day: date | None = None
    at: time | None = None

    @property
    def found(self) -> bool:
        return self.day is not None or self.at is not None


def guess_building(text: str) -> str | None:
    low = text.lower().replace("\u200c", " ")
    for key, words in KEYWORDS.items():
        if any(w in low for w in words):
            return key
    return None


def parse_when(text: str, today: date, now_time: time | None = None) -> When:
    s = text.translate(PERSIAN_DIGITS).replace("\u200c", " ").replace("ي", "ی")
    when = When()
    if "پس فردا" in s or "پسفردا" in s:
        when.day = today + timedelta(days=2)
    elif "فردا" in s:
        when.day = today + timedelta(days=1)
    elif "امروز" in s or "امشب" in s:
        when.day = today
    else:
        for name, wd in WEEKDAYS_FA.items():
            if name in s:
                ahead = (wd - today.weekday()) % 7 or 7
                when.day = today + timedelta(days=ahead)
                break
        else:
            if m := re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s):
                try:
                    when.day = date(int(m[1]), int(m[2]), int(m[3]))
                except ValueError:
                    pass

    m = re.search(r"(?:ساعت\s*)(\d{1,2})(?:[:.](\d{2}))?", s) or re.search(r"\b(\d{1,2}):(\d{2})\b", s)
    hour = minute = None
    if m:
        hour, minute = int(m[1]), int(m[2] or 0)
    elif m := re.search(r"(\d{1,2})\s*(صبح|ظهر|بعدازظهر|بعد از ظهر|عصر|غروب|شب)", s):
        hour, minute = int(m[1]), 0
    if hour is not None and hour <= 23 and minute <= 59:
        pm = any(w in s for w in ("عصر", "بعدازظهر", "بعد از ظهر", "غروب", "شب", "امشب"))
        if (pm and hour < 12) or ("ظهر" in s and hour < 5 and "بعد" not in s):
            hour += 12 if hour < 12 else 0
        elif not pm and "صبح" not in s and 1 <= hour <= 7:
            hour += 12  # «ساعت ۶» with no part of day: evening is the usual meaning
        when.at = time(hour % 24, minute)
    elif "امشب" in s:
        when.at = time(21, 0)
    elif "عصر" in s:
        when.at = time(18, 0)
    elif "صبح" in s:
        when.at = time(10, 0)
    elif "ظهر" in s:
        when.at = time(13, 0)

    if when.at and when.day is None:
        when.day = today if now_time is None or when.at > now_time else today + timedelta(days=1)
    return when
