"""Telegram front door of City Hall."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config as config_mod
from . import learning
from .ai import make_ai
from .buildings import label
from .city import CityHall, Outgoing
from .db import DB

log = logging.getLogger(__name__)

HELP = """🏛 شهرداری OCity

هر چیزی بنویسی، می‌فهمم و توی ساختمون درستش ثبت می‌کنم. مثلاً:
«پنجشنبه ساعت ۶ به Sea Club زنگ بزنم»
«آپدیت ووکامرس انجام شد»
«جمعه‌ها کار سایت نمی‌کنم»

دستورها:
/today گزارش امروز
/tasks همه کارهای باز
/city وضعیت شهر و طبقه‌ها
/profile چیزهایی که از روال کارت یاد گرفتم
/plan برنامه محتوا رو بعد از این دستور بفرست (متن یا فایل txt)
/script 12 سناریوی کار شماره ۱۲
/sprint شروع دوباره اسپرینت محتوا"""


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
    ])


def done_keyboard(task_ids: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"✅ #{i}", callback_data=f"d:{i}") for i in task_ids[j:j + 3]]
            for j in range(0, len(task_ids), 3)]
    return InlineKeyboardMarkup(rows)


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
        self.awaiting_plan = False

    # --- owner -----------------------------------------------------------
    def owner_id(self) -> int | None:
        if self.cfg.owner_chat_id:
            return self.cfg.owner_chat_id
        stored = self.db.get_state("owner_chat_id")
        return int(stored) if stored else None

    def is_owner(self, update: Update) -> bool:
        return update.effective_chat is not None and update.effective_chat.id == self.owner_id()

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
            "قدم اول: برنامه محتوای ماهانه و جدول محتوای آماده‌ت رو با /plan برام بفرست.\n\n" + HELP
        )

    async def help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await update.message.reply_text(HELP)

    async def today(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            text = await asyncio.to_thread(self.city.morning_brief, now_utc())
            await update.message.reply_text(text)

    async def tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await update.message.reply_text(self.city.tasks_view(now_utc())[:4000])

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
            self.awaiting_plan = True
            await update.message.reply_text(
                "برنامه محتوا رو همینجا بفرست، یا اگه طولانیه به‌صورت فایل txt. هر محتوا یه خط:\n"
                "2026-11-05 | کراکس | راهنمای سایز | ریل | کلاسیک مشکی و سفید\n"
                "(تاریخ | بیزنس | عنوان | فرمت | آماده‌سازی)\n\n"
                "هر چی زیر هر خط بنویسی تا خط بعدی، سناریوی همون محتواست و روز ضبطش بهت می‌دم."
            )

    async def script(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        arg = update.message.text.partition(" ")[2].strip().lstrip("#")
        task = self.db.get_task(int(arg)) if arg.isdigit() else None
        if task is None:
            await update.message.reply_text("شماره کار رو بعد از دستور بنویس، مثلاً /script 12 (شماره‌ها توی /tasks هست).")
            return
        await update.message.reply_text(self.city.script_text(task), reply_markup=task_keyboard(task.id))

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
        await update.message.reply_text("دارم برنامه رو می‌خونم...")
        try:
            result = await asyncio.to_thread(self.city.import_content_plan, text, now_utc())
        except Exception:
            log.exception("content plan import failed")
            await update.message.reply_text("نتونستم برنامه رو بخونم. یه بار دیگه بفرست یا ساده‌ترش کن.")
            return
        await update.message.reply_text(f"{result.reply}\n\n{len(result.added)} کار توی 🎬 Media Hub ثبت شد.")

    # --- free text -------------------------------------------------------
    async def text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        message = update.message.text
        if self.awaiting_plan:
            self.awaiting_plan = False
            await self.import_plan(update, message)
            return
        result = await asyncio.to_thread(self.city.handle_message, message, now_utc())
        lines = [result.reply]
        for t in result.added:
            when = f" ({t.due_date}{' ' + t.due_time if t.due_time else ''})" if t.due_date else ""
            lines.append(f"• {label(t.building)}: {t.title}{when}")
        await update.message.reply_text("\n".join(lines))

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
        task = self.db.get_task(task_id)
        if task is None:
            await query.answer("این کار پیدا نشد")
            return
        now = now_utc()
        if action == "d":
            if task.status != "done":
                self.city.done(task_id, now)
            await query.answer("یه طبقه اضافه شد 🏗")
            await query.message.reply_text(
                f"✅ {task.title}\n{label(task.building)} یه طبقه بالاتر رفت. چقدر طول کشید؟",
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
        elif action == "m":
            minutes = int(rest[0])
            if minutes:
                self.city.set_duration(task_id, minutes)
            await query.answer("ثبت شد" if minutes else "باشه")
            await query.edit_message_reply_markup(None)

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
                markup = task_keyboard(m.task_id)
            elif m.done_buttons:
                markup = done_keyboard([t.id for t in m.done_buttons])
            await ctx.bot.send_message(chat_id, m.text, reply_markup=markup)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load()
    if not cfg.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(cfg)
    app = Application.builder().token(cfg.telegram_token).build()
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help", bot.help))
    app.add_handler(CommandHandler("today", bot.today))
    app.add_handler(CommandHandler("tasks", bot.tasks))
    app.add_handler(CommandHandler("city", bot.city_cmd))
    app.add_handler(CommandHandler("profile", bot.profile))
    app.add_handler(CommandHandler("plan", bot.plan))
    app.add_handler(CommandHandler("sprint", bot.sprint))
    app.add_handler(CommandHandler("script", bot.script))
    app.add_handler(CallbackQueryHandler(bot.button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot.voice))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.text))
    app.job_queue.run_repeating(bot.tick, interval=60, first=5)
    log.info("City Hall is running (AI %s)", "on" if cfg.ai_enabled else "off")
    app.run_polling()


if __name__ == "__main__":
    main()
