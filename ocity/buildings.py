"""The buildings of the city and the starter tasks seeded on first /start."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Building:
    key: str
    name: str
    emoji: str
    bridges: tuple[str, ...] = ()
    district: str = "tech"
    base: int = 0  # floors it already had before OCity started counting (a running business is not an empty lot)


# The city's districts, in the order the Mini App lists them.
DISTRICTS: dict[str, tuple[str, str, str]] = {  # key -> (name, emoji, what lives there)
    "downtown": ("مرکز شهر", "💰", "درآمد امروز: کارهایی که همین الان پول می‌سازن"),
    "tech": ("شهرک فناوری", "🚀", "درآمد فردا: SmartO و محصولاتش"),
    "support": ("منطقه پشتیبانی", "🛠", "به بقیه ساختمون‌ها خدمت می‌دن"),
    "center": ("میدون مرکزی", "🏛", "شهرداری، وصل به همه"),
    "personal": ("محله شخصی", "🏡", "زندگی، سلامتی، خانواده"),
    "future": ("زمین‌های آینده", "🌱", "ایده‌ها، تا وقتی فعال بشن"),
}

BUILDINGS: dict[str, Building] = {
    b.key: b
    for b in [
        Building("cityhall", "شهرداری", "🏛", ("retail", "media", "smarto", "marketing"), "center"),
        Building("retail", "کراکس منیجمنت", "👟", ("retailos", "media", "marketing"), "downtown", 10),
        Building("technician", "کار تکنسینی", "🦷", ("techos", "knowledge"), "downtown", 4),
        Building("tradebot", "ترید بات", "📈", (), "downtown", 3),
        Building("smarto", "SmartO", "🤖", ("retailos", "techos", "fitness", "marketing", "media"), "tech"),
        Building("retailos", "Retail OS", "🛒", ("smarto", "retail"), "tech"),
        Building("techos", "Technician OS", "🔧", ("smarto", "technician", "knowledge"), "tech"),
        Building("fitness", "Fitness OS", "💪", ("smarto", "marketing", "media"), "tech"),
        Building("media", "Media Hub", "🎬", ("marketing", "retail", "smarto", "fitness"), "support"),
        Building("marketing", "برج مارکتینگ", "📣", ("media", "smarto", "fitness", "retail"), "support"),
        Building("knowledge", "دانش دوره‌ها", "📚", ("technician", "techos"), "support"),
        Building("personal", "زندگی شخصی", "🏠", (), "personal"),
        Building("vault", "گنجینه ایده‌ها", "💡", (), "future"),
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
        district = s.get("district") if s.get("district") in DISTRICTS else (base.district if base else "tech")
        BUILDINGS[key] = Building(key, name, emoji, base.bridges if base else (), district, base.base if base else 0)


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
