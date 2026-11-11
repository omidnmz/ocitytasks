"""Telegram front door of City Hall."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config as config_mod
from . import learning, pipeline
from .ai import make_ai
from .buildings import BUILDINGS, label
from .capture import guess_building
from .city import CityHall, Outgoing
from .db import DB

log = logging.getLogger(__name__)

HELP = """🏛 شهرداری OCity

هر کاری که یادت افتاد رو همینجا بنویس. می‌ذارمش توی ساختمون درستش و اگه زمانش رو نگفته باشی، می‌پرسم کی یادت بندازم. مثلاً:
«پنجشنبه ساعت ۶ به Sea Club زنگ بزنم»
«ایده: ویدیوی پشت صحنه کلینیک»
«آپدیت ووکامرس انجام شد»
«جمعه‌ها کار سایت نمی‌کنم»

دستورها:
دکمه‌های پایین صفحه همین کارها رو می‌کنن. توی «کارهای امروز» روی هر کار بزن تا انجامش بدی، عقبش بندازی یا حذفش کنی.

/tasks کارهای امروز
/buildings قدم بعدی هر ساختمون
/today گزارش کامل امروز
/all همه کارهای باز
/city وضعیت شهر و طبقه‌ها
/profile چیزهایی که از روال کارت یاد گرفتم
/plan متن آماده برای چت مارکتینگ هر برند، و گرفتن برنامه‌شون
/script 12 سناریوی کار شماره ۱۲
/del 12 پاک کردن کار شماره ۱۲

چرخه هفتگی محتوا:
جمعه: 📣 برج مارکتینگ برای هر برند برنامه هفته بعد رو می‌گیره (جوابِ چت مارکتینگ رو همینجا بفرست).
خودم استوری‌ها رو می‌دم به روزها، ریلزها رو می‌برم توی خط تولید Media Hub و بقیه کارها رو بین ساختمون‌ها پخش می‌کنم.
هر روز صبح استوری‌های اون روز رو از تالار شهر می‌گیری.
یکشنبه فیلم‌برداری دسته‌ای (شبِ قبلش خبرت می‌کنم)، دوشنبه و سه‌شنبه تدوین، بعد انتشار.
هدف: همیشه ۳ محتوای آماده‌ی انتشار جلوتر باشیم."""


# The menu that stays under the chat, so nothing has to be typed as a command
MENU_TODAY = "📌 کارهای امروز"
MENU_BUILDINGS = "🏗 ساختمون‌ها"
MENU_BRIEF = "☀️ گزارش کامل"
MENU_CITY = "🏙 شهر"
MENU_ALL = "📋 همه کارها"
MENU_PLAN = "📣 برنامه مارکتینگ"
MENU_PROFILE = "🧠 روال من"
MENU_HELP = "❓ راهنما"
MENU_APP = "🏙 باز کردن شهر"


def make_menu(webapp_url: str = "") -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(MENU_TODAY), KeyboardButton(MENU_BUILDINGS)],
        [KeyboardButton(MENU_BRIEF), KeyboardButton(MENU_ALL)],
        [KeyboardButton(MENU_PLAN), KeyboardButton(MENU_CITY)],
        [KeyboardButton(MENU_PROFILE), KeyboardButton(MENU_HELP)],
    ]
    if webapp_url:
        rows.insert(0, [KeyboardButton(MENU_APP, web_app=WebAppInfo(url=webapp_url))])
    return ReplyKeyboardMarkup(
        rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="کار جدید رو بنویس...",
    )


COMMANDS = [
    BotCommand("tasks", "کارهای امروز"),
    BotCommand("buildings", "قدم بعدی ساختمون‌ها"),
    BotCommand("today", "گزارش کامل امروز"),
    BotCommand("all", "همه کارهای باز"),
    BotCommand("plan", "برنامه مارکتینگ هفته"),
    BotCommand("city", "وضعیت شهر"),
    BotCommand("profile", "روال کاری من"),
    BotCommand("menu", "نشون دادن منو"),
    BotCommand("help", "راهنما"),
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def task_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ انجام شد", callback_data=f"d:{task_id}"),
            InlineKeyboardButton("⏰ ۳۰ دقیقه", callback_data=f"s:{task_id}:30"),
            InlineKeyboardButton("⏰ یه ساعت", callback_data=f"s:{task_id}:60"),
        ],
        [
            InlineKeyboardButton("🌙 امشب", callback_data=f"s:{task_id}:tonight"),
            InlineKeyboardButton("📅 فردا", callback_data=f"s:{task_id}:tomorrow"),
            InlineKeyboardButton("🧱 گیر کردم", callback_data=f"b:{task_id}"),
            InlineKeyboardButton("👥 پرسنل", callback_data=f"g:{task_id}"),
        ],
        [InlineKeyboardButton("🗑 حذف این کار", callback_data=f"r:{task_id}")],
    ])


def stuck_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ انجام شد", callback_data=f"d:{task_id}"),
            InlineKeyboardButton("✂️ کوچیکش کن", callback_data=f"x:{task_id}"),
        ],
        [
            InlineKeyboardButton("👥 پرسنل", callback_data=f"g:{task_id}"),
            InlineKeyboardButton("📅 فردا", callback_data=f"s:{task_id}:tomorrow"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"r:{task_id}"),
        ],
    ])


REMIND_OPTIONS = [
    ("⏰ یه ساعت دیگه", "1h"), ("🌆 عصر ۱۸", "evening"), ("🌙 امشب ۲۱", "tonight"),
    ("☀️ فردا", "tomorrow"), ("📅 این هفته", "week"), ("🚫 یادآوری نمی‌خواد", "none"),
]
REMIND_QUESTION = "کی یادت بندازم؟ یکی رو بزن یا خودت بنویس، مثلاً «پنجشنبه ساعت ۵»."


def remind_keyboard(task_id: int) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text, callback_data=f"w:{task_id}:{opt}") for text, opt in REMIND_OPTIONS]
    return InlineKeyboardMarkup([
        buttons[:3], buttons[3:],
        [InlineKeyboardButton("🏷 تغییر ساختمون", callback_data=f"c:{task_id}:?")],
    ])


def building_keyboard(task_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(f"{b.emoji} {b.name}", callback_data=f"c:{task_id}:{key}")
        for key, b in BUILDINGS.items()
    ]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def today_keyboard(tasks) -> InlineKeyboardMarkup:
    """One button per task; tapping it opens that task's done/snooze options."""
    rows = []
    for t in tasks:
        title = t.title if len(t.title) <= 34 else t.title[:32] + "…"
        rows.append([InlineKeyboardButton(f"{BUILDINGS[t.building].emoji} {title}", callback_data=f"o:{t.id}")])
    return InlineKeyboardMarkup(rows)


CLEANUP_LIMIT = 60


def cleanup_keyboard(tasks) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks[:CLEANUP_LIMIT]:
        title = t.title if len(t.title) <= 34 else t.title[:32] + "…"
        rows.append([InlineKeyboardButton(f"🗑 #{t.id} {title}", callback_data=f"k:{t.id}")])
    return InlineKeyboardMarkup(rows)


def done_keyboard(task_ids: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"✅ #{i}", callback_data=f"d:{i}") for i in task_ids[j:j + 3]]
            for j in range(0, len(task_ids), 3)]
    return InlineKeyboardMarkup(rows)


STAGE_START = {"filming": "🎬 شروع فیلم‌برداری", "editing": "✂️ شروع تدوین"}
STAGE_WORK = {"filming": "فیلم‌برداری", "editing": "تدوین", "publish": "انتشار"}


def start_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(STAGE_START.get(kind, "▶️ شروع"), callback_data=f"f:0:{kind}")]])


def focus_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ گرفتمش", callback_data=f"n:{task_id}"),
        InlineKeyboardButton("⏭ بعدی", callback_data=f"p:{task_id}"),
    ]])


def time_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label_, callback_data=f"t:0:{kind}:{at.replace(':', '')}")
        for at, label_ in CityHall.TIME_CHOICES
    ]])


def duration_keyboard(task_id: int) -> InlineKeyboardMarkup:
    options = [15, 30, 45, 60, 90, 120]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{m}′", callback_data=f"m:{task_id}:{m}") for m in options[:3]],
        [InlineKeyboardButton(f"{m}′", callback_data=f"m:{task_id}:{m}") for m in options[3:]]
        + [InlineKeyboardButton("رد", callback_data=f"m:{task_id}:0")],
    ])


class Bot:
    def __init__(self, cfg: config_mod.Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.city = CityHall(self.db, cfg, make_ai(cfg.anthropic_api_key, cfg.claude_model))
        self.menu_markup = make_menu(cfg.webapp_url)
        self.awaiting_plan = False
        self.awaiting_when: int | None = None  # task waiting for a typed reminder time
        self.awaiting_split: int | None = None  # stuck task waiting for its first small step
        self.asking_when: set[int] = set()  # tasks whose building picker should lead to the time question
        self.focus: dict | None = None  # the filming/editing session: kind, pieces done, pieces skipped

    # --- owner -----------------------------------------------------------
    def owner_id(self) -> int | None:
        if self.cfg.owner_chat_id:
            return self.cfg.owner_chat_id
        stored = self.db.get_state("owner_chat_id")
        return int(stored) if stored else None

    def is_owner(self, update: Update) -> bool:
        owner = update.effective_chat is not None and update.effective_chat.id == self.owner_id()
        if owner:
            self.city.seen(now_utc())
        return owner

    # --- commands --------------------------------------------------------
    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if self.owner_id() is None:
            self.db.set_state("owner_chat_id", str(chat_id))
        if not self.is_owner(update):
            await update.message.reply_text("این ربات شخصیه.")
            return
        self.city.seed(now_utc())
        await update.message.reply_text(
            "سلام شهردار! 🏛 شهرداری راه افتاد.\n\n"
            "چند کار شروع رو از وضعیت فعلیت ثبت کردم و اسپرینت سه‌روزه محتوا رو شروع کردم "
            "تا برنامه و تولید محتوای چند روزت رو یکجا جلو ببریم.\n\n"
            "قدم اول: برنامه محتوای ماهانه و جدول محتوای آماده‌ت رو با /plan برام بفرست.\n\n" + HELP,
            reply_markup=self.menu_markup,
        )

    async def help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await update.message.reply_text(HELP, reply_markup=self.menu_markup)

    async def menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await update.message.reply_text("منو پایین صفحه‌ست 👇", reply_markup=self.menu_markup)

    async def buildings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await update.message.reply_text(self.city.buildings_view(now_utc())[:4000])

    async def today(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            text = await asyncio.to_thread(self.city.morning_brief, now_utc())
            await update.message.reply_text(text)

    async def tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            text, items = self.city.today_view(now_utc())
            await update.message.reply_text(text, reply_markup=today_keyboard(items) if items else None)

    async def all_tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        text = self.city.all_view(now_utc())
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]  # long lists go in several messages
        cleanup = InlineKeyboardMarkup([[InlineKeyboardButton("🧹 پاک کردن کارهای اضافه", callback_data="q:0")]])
        for i, chunk in enumerate(chunks):
            await update.message.reply_text(chunk, reply_markup=cleanup if i == len(chunks) - 1 else None)

    async def city_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await update.message.reply_text(self.city.city_view(now_utc()))

    async def profile(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        text = "🧠 روال کاری تو:\n\n" + learning.describe(self.city.profile())
        rules = self.db.rules()
        if rules:
            text += "\n\n📜 قانون‌هایی که گفتی:\n" + "\n".join(f"• {r}" for r in rules)
        await update.message.reply_text(text)

    async def plan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        body = update.message.text.partition(" ")[2].strip()
        if body:
            await self.import_plan(update, body)
        else:
            await self.plan_prompt(update, ctx)

    async def plan_prompt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        self.awaiting_plan = True
        await update.message.reply_text(
            "📣 برج مارکتینگ: متن‌های پایین رو توی چت مارکتینگ هر برند توی Claude بچسبون "
            "و جوابشون رو همینجا بفرست (یکی‌یکی یا همه با هم، یا فایل txt).\n"
            "خودم استوری‌ها رو می‌دم به روزها، ریلزها رو می‌برم توی خط تولید Media Hub و بقیه کارها رو پخش می‌کنم."
        )
        for text in self.city.marketing_prompts(now_utc()):
            await update.message.reply_text(text)

    async def script(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        arg = update.message.text.partition(" ")[2].strip().lstrip("#")
        task = self.db.get_task(int(arg)) if arg.isdigit() else None
        if task is None:
            await update.message.reply_text("شماره کار رو بعد از دستور بنویس، مثلاً /script 12 (شماره‌ها توی /tasks هست).")
            return
        await update.message.reply_text(self.city.script_text(task), reply_markup=task_keyboard(task.id))

    async def delete(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        ids = [int(a.lstrip("#")) for a in update.message.text.split()[1:] if a.lstrip("#").isdigit()]
        removed = []
        for task_id in ids:
            task = self.db.get_task(task_id)
            if task and task.status == "open":
                self.city.drop(task_id, now_utc())
                removed.append(f"#{task_id}")
        if removed:
            await update.message.reply_text("🗑 پاک شد: " + " ".join(removed))
        else:
            await update.message.reply_text("شماره کار رو بعد از دستور بنویس، مثلاً /del 12 15 (شماره‌ها توی /tasks هست).")

    async def document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A .txt or .md file is a content plan handoff."""
        if not self.is_owner(update):
            return
        doc = update.message.document
        name = (doc.file_name or "").lower()
        if not name.endswith((".txt", ".md")):
            await update.message.reply_text("فعلاً فقط فایل txt یا md رو می‌تونم به‌عنوان برنامه محتوا بخونم.")
            return
        data = await (await doc.get_file()).download_as_bytearray()
        self.awaiting_plan = False
        await self.import_plan(update, bytes(data).decode("utf-8-sig", errors="replace"))

    async def sprint(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            self.city.start_content_sprint(now_utc())
            await update.message.reply_text("🎬 اسپرینت سه‌روزه محتوا دوباره شروع شد. روزی سه بار پیگیرت می‌شم.")

    async def import_plan(self, update: Update, text: str) -> None:
        marketing = pipeline.is_marketing_plan(text)
        await update.message.reply_text("📣 برج مارکتینگ داره برنامه رو باز می‌کنه..." if marketing
                                        else "دارم برنامه رو می‌خونم...")
        try:
            if marketing:
                result = await asyncio.to_thread(self.city.import_marketing_plan, text, now_utc())
            else:
                result = await asyncio.to_thread(self.city.import_content_plan, text, now_utc())
        except Exception:
            log.exception("content plan import failed")
            await update.message.reply_text("نتونستم برنامه رو بخونم. یه بار دیگه بفرست یا ساده‌ترش کن.")
            return
        if marketing:
            await update.message.reply_text(result.reply)
        else:
            await update.message.reply_text(f"{result.reply}\n\n{len(result.added)} کار توی 🎬 Media Hub ثبت شد.")
        for kind in result.ask_times:
            await update.message.reply_text(
                f"🏛 {STAGE_WORK.get(kind, kind)}‌ها رو معمولاً کی انجام می‌دی؟ همون موقع یادت میندازم.",
                reply_markup=time_keyboard(kind))

    # --- free text -------------------------------------------------------
    async def text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        message = update.message.text
        menu = {
            MENU_TODAY: self.tasks, MENU_BUILDINGS: self.buildings, MENU_BRIEF: self.today,
            MENU_CITY: self.city_cmd, MENU_ALL: self.all_tasks, MENU_PROFILE: self.profile,
            MENU_HELP: self.help, MENU_PLAN: self.plan_prompt, "🎬 برنامه محتوا": self.plan_prompt,
        }
        if message in menu:
            self.awaiting_when = self.awaiting_split = None
            await menu[message](update, ctx)
            return
        if self.awaiting_plan or pipeline.is_marketing_plan(message):
            self.awaiting_plan = False
            await self.import_plan(update, message)
            return
        if self.awaiting_split is not None:
            task_id, self.awaiting_split = self.awaiting_split, None
            step = self.city.split(task_id, message, now_utc())
            await update.message.reply_text(
                f"✂️ قدم اول ثبت شد: {step.title}\nکار بزرگ رفت توی در حال ساخت.\n\n{REMIND_QUESTION}",
                reply_markup=remind_keyboard(step.id),
            )
            self.awaiting_when = step.id
            return
        if self.awaiting_when is not None:
            task_id, self.awaiting_when = self.awaiting_when, None
            # a short answer like «پنجشنبه ساعت ۵» is the time; anything longer is a new thought
            short = len(message.split()) <= 4 and guess_building(message) is None
            when = self.city.remind_from_text(task_id, message, now_utc()) if short else None
            if when:
                await update.message.reply_text(f"⏰ باشه، {when} یادت میندازم.")
                return
            # not a time: it is a new thought
        result = await asyncio.to_thread(self.city.handle_message, message, now_utc())
        lines = [result.reply]
        if self.city.ai:
            for t in result.added:
                when = f" ({t.due_date}{' ' + t.due_time if t.due_time else ''})" if t.due_date else ""
                lines.append(f"• {label(t.building)}: {t.title}{when}")
        markup = None
        task = result.added[0] if len(result.added) == 1 else None
        if task and result.ask_building:
            lines.append("\nکدوم ساختمون؟")
            markup = building_keyboard(task.id)
            if result.ask_when:
                self.asking_when.add(task.id)
        elif task and result.ask_when:
            lines.append("\n" + REMIND_QUESTION)
            markup = remind_keyboard(task.id)
            self.awaiting_when = task.id
        elif task and task.board != "construction":
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🏷 تغییر ساختمون", callback_data=f"c:{task.id}:?")]])
        await update.message.reply_text("\n".join(lines), reply_markup=markup)

    async def voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await update.message.reply_text("ویس هنوز توی این نسخه فعال نیست. فعلاً متنی بنویس، به‌زودی اضافه می‌شه.")

    # --- buttons ---------------------------------------------------------
    async def button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.is_owner(update):
            await query.answer()
            return
        action, task_id, *rest = query.data.split(":")
        task_id = int(task_id)
        if action == "q":
            await query.answer()
            tasks = self.db.open_tasks()
            more = f"\n(فقط {CLEANUP_LIMIT} تای اول نشون داده شده)" if len(tasks) > CLEANUP_LIMIT else ""
            await query.message.reply_text(
                "روی هر کاری که اضافه‌ست بزن تا حذف بشه. بقیه دست نمی‌خورن." + more,
                reply_markup=cleanup_keyboard(tasks),
            )
            return
        if action == "k":
            task = self.db.get_task(task_id)
            if task and task.status == "open":
                self.city.drop(task_id, now_utc())
            await query.answer("🗑 حذف شد")
            rows = [
                row for row in query.message.reply_markup.inline_keyboard
                if row[0].callback_data != f"k:{task_id}"
            ]
            await query.edit_message_reply_markup(InlineKeyboardMarkup(rows) if rows else None)
            return
        if action == "t":
            kind, at = rest[0], f"{rest[1][:2]}:{rest[1][2:]}"
            self.city.set_kind_time(kind, at)
            await query.answer("ثبت شد")
            await query.edit_message_text(f"🏛 باشه، {STAGE_WORK.get(kind, kind)}‌ها ساعت {at}. همون موقع خبرت می‌کنم.")
            return
        if action == "f":
            await query.answer()
            self.focus = {"kind": rest[0], "done": 0, "skipped": set()}
            await self.focus_next(query.message)
            return
        task = self.db.get_task(task_id)
        if task is None:
            await query.answer("این کار پیدا نشد")
            return
        now = now_utc()
        if action in ("n", "p"):
            if self.focus is None:
                self.focus = {"kind": task.kind, "done": 0, "skipped": set()}
            if action == "n" and task.status == "open":
                self.city.done(task_id, now)
                self.focus["done"] += 1
                await query.answer("یه طبقه به Media Hub اضافه شد 🏗")
                await query.edit_message_text(f"✅ {task.title}")
            else:
                self.focus["skipped"].add(task_id)
                await query.answer("رفت برای بعد")
                await query.edit_message_reply_markup(None)
            await self.focus_next(query.message)
            return
        if action == "d":
            step = ""
            if task.status != "done":
                step = getattr(self.city.done(task_id, now), "next_step", "")
            await query.answer("یه طبقه اضافه شد 🏗")
            await query.message.reply_text(
                f"✅ {task.title}\n{label(task.building)} یه طبقه بالاتر رفت."
                + (f"\n🏛 {step}" if step else "") + "\nچقدر طول کشید؟",
                reply_markup=duration_keyboard(task_id),
            )
        elif action == "s":
            when = self.city.snooze(task_id, rest[0], now)
            await query.answer("باشه")
            await query.edit_message_text(
                f"{query.message.text}\n\n⏰ دوباره می‌پرسم: {self.city.local(when):%a %H:%M}"
            )
        elif action == "b":
            self.city.block(task_id, now)
            await query.answer()
            await query.edit_message_text(f"{query.message.text}\n\n🧱 رفت توی در انتظار. چی لازمه تا باز بشه؟ برام بنویس.")
        elif action == "g":
            self.city.delegate(task_id, now)
            await query.answer()
            await query.edit_message_text(f"{query.message.text}\n\n👥 سپرده شد به پرسنل. فردا صبح پیگیرش می‌شم.")
        elif action == "o":
            await query.answer()
            if task.status != "open":
                await query.message.reply_text(f"«{task.title}» دیگه باز نیست.")
                return
            text = f"{label(task.building)}\n{task.title}"
            if task.due_time:
                text += f"\n⏰ {task.due_time}"
            if task.notes:
                text += f"\n\n📝 {task.notes}"
            await query.message.reply_text(text[:4000], reply_markup=task_keyboard(task_id))
        elif action == "w":
            self.awaiting_when = None
            when = self.city.remind(task_id, rest[0], now)
            await query.answer("باشه")
            text = f"⏰ {when} یادت میندازم." if rest[0] != "none" else "🏗 بدون یادآوری، رفت توی صف ساختمونش."
            await query.edit_message_text(f"✅ {label(task.building)}: {task.title}\n{text}")
        elif action == "c":
            await query.answer()
            if rest[0] == "?":
                await query.edit_message_reply_markup(building_keyboard(task_id))
                return
            task = self.city.set_building(task_id, rest[0])
            text = f"✅ {label(task.building)}: {task.title}"
            if task_id in self.asking_when or self.awaiting_when == task_id:
                self.asking_when.discard(task_id)
                self.awaiting_when = task_id
                await query.edit_message_text(f"{text}\n\n{REMIND_QUESTION}", reply_markup=remind_keyboard(task_id))
            else:
                await query.edit_message_text(text)
        elif action == "x":
            self.awaiting_split = task_id
            await query.answer()
            await query.message.reply_text(
                f"«{task.title}»\nاولین قدم ۱۵ دقیقه‌ای‌ش چیه؟ همینجا بنویس، مثلاً «فقط لیست کلینیک‌ها رو باز کنم»."
            )
        elif action == "r":
            self.city.drop(task_id, now)
            await query.answer("حذف شد")
            await query.edit_message_text(f"{query.message.text}\n\n🗑 حذف شد. یه بار از ذهنت هم برداشتیمش.")
        elif action == "m":
            minutes = int(rest[0])
            if minutes:
                self.city.set_duration(task_id, minutes)
            await query.answer("ثبت شد" if minutes else "باشه")
            await query.edit_message_reply_markup(None)

    async def focus_next(self, message) -> None:
        """Filming or editing mode: one piece at a time with its script, then City Hall's wrap-up."""
        f = self.focus
        queue = [t for t in self.city.stage_queue(f["kind"], now_utc()) if t.id not in f["skipped"]]
        if queue:
            await message.reply_text(self.city.focus_text(queue[0], len(queue)), reply_markup=focus_keyboard(queue[0].id))
            return
        self.focus = None
        work = STAGE_WORK.get(f["kind"], f["kind"])
        if not f["done"]:
            await message.reply_text(f"🏛 فعلاً چیزی برای {work} نمونده.")
            return
        nxt = "✂️ تدوین" if f["kind"] == "filming" else "📤 انتشار"
        later = [t for t in self.db.open_tasks() if t.kind == ("editing" if f["kind"] == "filming" else "publish")
                 and t.board == "today" and t.due_date]
        when = min(t.due_date for t in later) if later else None
        at = self.city.kind_time("editing" if f["kind"] == "filming" else "publish")
        left = f" ({len(f['skipped'])} تا موند برای بعد)" if f["skipped"] else ""
        await message.reply_text(
            f"🏛 تالار شهر: {work} تموم شد ✅ {f['done']} محتوا{left}، {f['done']} طبقه به 🎬 Media Hub اضافه شد.\n"
            + (f"مرحله بعد: {nxt}، {when}{' ساعت ' + at if at else ''}. روزش خبرت می‌کنم." if when else "")
        )

    # --- the minute tick -------------------------------------------------
    async def tick(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = self.owner_id()
        if chat_id is None:
            return
        try:
            messages: list[Outgoing] = await asyncio.to_thread(self.city.tick, now_utc())
        except Exception:
            log.exception("tick failed")
            return
        for m in messages:
            markup = None
            if m.task_id:
                markup = stuck_keyboard(m.task_id) if m.stuck else task_keyboard(m.task_id)
            elif m.done_buttons:
                markup = done_keyboard([t.id for t in m.done_buttons])
            elif m.start_kind:
                markup = start_keyboard(m.start_kind)
            await ctx.bot.send_message(chat_id, m.text, reply_markup=markup)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load()
    if not cfg.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(cfg)
    runner = None

    async def post_init(app: Application) -> None:
        nonlocal runner
        await app.bot.set_my_commands(COMMANDS)
        if cfg.webapp_url:
            from . import web  # aiohttp is only needed when the Mini App is on

            runner = await web.start(bot.city, cfg.telegram_token, bot.owner_id, cfg.web_host, cfg.web_port)
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🏙 شهر", web_app=WebAppInfo(url=cfg.webapp_url))
            )
        version = "3" if cfg.webapp_url else "3-text"  # bump when the menu's buttons change
        if bot.owner_id() and bot.db.get_state("menu_version") != version:
            bot.db.set_state("menu_version", version)
            text = ("🏛 تالار شهر آپدیت شد: دکمه «📣 برنامه مارکتینگ» متن آماده چت مارکتینگ هر برند رو می‌ده. "
                    "جوابشون رو که بفرستی، خودم کارها رو بین Media Hub و بقیه ساختمون‌ها پخش می‌کنم.")
            await app.bot.send_message(bot.owner_id(), text, reply_markup=bot.menu_markup)

    async def post_shutdown(app: Application) -> None:
        if runner:
            await runner.cleanup()

    app = (
        Application.builder().token(cfg.telegram_token)
        .post_init(post_init).post_shutdown(post_shutdown).build()
    )
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help", bot.help))
    app.add_handler(CommandHandler("today", bot.today))
    app.add_handler(CommandHandler("tasks", bot.tasks))
    app.add_handler(CommandHandler("all", bot.all_tasks))
    app.add_handler(CommandHandler("buildings", bot.buildings))
    app.add_handler(CommandHandler("menu", bot.menu))
    app.add_handler(CommandHandler("city", bot.city_cmd))
    app.add_handler(CommandHandler("profile", bot.profile))
    app.add_handler(CommandHandler("plan", bot.plan))
    app.add_handler(CommandHandler("sprint", bot.sprint))
    app.add_handler(CommandHandler("script", bot.script))
    app.add_handler(CommandHandler("del", bot.delete))
    app.add_handler(CallbackQueryHandler(bot.button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot.voice))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.text))
    app.job_queue.run_repeating(bot.tick, interval=60, first=5)
    log.info("City Hall is running (AI %s)", "on" if cfg.ai_enabled else "off")
    app.run_polling()


if __name__ == "__main__":
    main()
