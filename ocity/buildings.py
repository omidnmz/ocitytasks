"""The buildings of the city and the starter tasks seeded on first /start."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Building:
    key: str
    name: str
    emoji: str
    bridges: tuple[str, ...] = ()


BUILDINGS: dict[str, Building] = {
    b.key: b
    for b in [
        Building("cityhall", "شهرداری", "🏛", ("retail", "media", "smarto", "marketing")),
        Building("retail", "Retail OS / کراکس", "👟", ("media", "smarto", "marketing")),
        Building("media", "Media Hub", "🎬", ("marketing", "retail", "smarto", "fitness")),
        Building("marketing", "برج مارکتینگ", "📣", ("media", "smarto", "fitness", "retail")),
        Building("smarto", "SmartO", "🤖", ("marketing", "media", "technician")),
        Building("technician", "تکنسینی", "🦷", ("smarto",)),
        Building("fitness", "Fitness OS", "💪", ("marketing", "media")),
        Building("tradebot", "ترید بات", "📈", ()),
        Building("personal", "زندگی شخصی", "🏠", ()),
        Building("vault", "گنجینه ایده‌ها", "💡", ()),
    ]
}

BUILDING_KEYS = tuple(BUILDINGS)
DEFAULT_BUILDINGS = dict(BUILDINGS)


def configure(settings: dict[str, dict]) -> None:
    """Apply the Mayor's renames, emoji changes and own buildings on top of the defaults."""
    BUILDINGS.clear()
    BUILDINGS.update(DEFAULT_BUILDINGS)
    for key, s in settings.items():
        base = BUILDINGS.get(key)
        name = (s.get("name") or (base.name if base else key)).strip()
        emoji = (s.get("emoji") or (base.emoji if base else "🏢")).strip()
        BUILDINGS[key] = Building(key, name, emoji, base.bridges if base else ())


def label(key: str) -> str:
    b = BUILDINGS.get(key)
    return f"{b.emoji} {b.name}" if b else key


# The content sprint: the first days push the Mayor to plan and produce
# several days of content in one go instead of thinking about it daily.
CONTENT_SPRINT_STEPS = [
    "برنامه محتوای ماهانه و جدول محتوای آماده رو با /plan برای ربات بفرست",
    "محتواهای ۷ روز آینده رو انتخاب کن (کدوم بیزنس، کدوم محصول، چه فرمتی)",
    "لیست محصولات و آماده‌سازی فیلم‌برداری رو بده به پرسنل",
    "یه بلوک فیلم‌برداری بذار و محتوای چند روز رو یکجا ضبط کن",
    "تدوین و موشن‌گرافی محتواهای ضبط‌شده",
    "زمان‌بندی انتشار محتواهای هفته",
]

# A small, real starting state (from the Mayor's status update), not a giant list.
STARTER_TASKS = [
    # (title, building, board, priority)
    ("پیگیری پرداخت مشتری کراکس که کارش تموم شده", "retail", "waiting", "high"),
    ("پیگیری مشتری دوم کراکس تا به نتیجه برسه", "retail", "today", "high"),
    ("خرید دامین SmartO", "smarto", "construction", "high"),
    ("نوشتن برنامه مارکتینگ برای هر محصول (SmartO، فیتنس، سایت کراکس)", "marketing", "construction", "high"),
    ("پیام به تسترهای Fitness OS و گرفتن بازخورد", "fitness", "construction", "normal"),
    ("گزارش روز اول تست ترید بات با حساب واقعی", "tradebot", "today", "normal"),
    ("لیست کلینیک‌های دندانپزشکی برای پیگیری هفتگی", "technician", "construction", "high"),
]
