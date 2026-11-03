# OCity City Hall

City Hall of OCity: a Telegram task manager that stays with you through the day,
follows up on tasks at the right moment, and learns how you work.

شهرداری OCity: یه دستیار تلگرامی که در طول روز همراهته، کارها رو سر وقت پیگیری
می‌کنه و کم‌کم روال کاری تو رو یاد می‌گیره.

## What it does

- **Morning brief** (default 10:30): top 3 tasks, one waiting item, overdue work, one thing not to do today.
- **Nudges** at a task's own time, or at the hour you usually do that kind of work once it has learned it.
  One tap answers: ✅ done, ⏰ 30 min / 1 hour / tonight / tomorrow, 🧱 stuck, 👥 give to staff.
- **Check-ins** (14:00 and 20:00) over today's tasks that have no time yet.
- **Content sprint**: for the first 3 days it follows up three times a day until several days of content are planned, filmed, edited and scheduled.
- **/plan**: paste a content plan and it becomes dated Media Hub tasks, with prep tasks for staff
  two days before. One line per piece works without Claude:
  `2026-11-05 | Crocs | size guide | reel | black and white classics` (date | business | title | format | prep).
  With a Claude key it also reads free-form plans.
  Lines under an item, up to the next item, are its script: the bot hands it over the day before
  publishing as a filming task, and on the publish day. A long plan can be sent as a `.txt` file.
  `docs/media-hub-claude-project.md` sets up a Claude project that writes this handoff each week.
- **/script 12**: show the script of task 12. **/del 12 15**: remove tasks 12 and 15.
  Sending the same plan again updates its tasks instead of duplicating them.
- **Evening wrap-up** (23:00): what got built, unfinished tasks move to tomorrow.
- **Learning**: every task logs when it was planned, when it was really done, how often it was pushed back and how long it took. `/profile` shows what it learned; tell it rules like «جمعه‌ها کار سایت نمی‌کنم».
- **The city**: each ✅ adds a floor to its building; overdue work puts scaffolding on it (`/city`).

Write anything in Persian and Claude turns it into tasks in the right building
(«پنجشنبه ساعت ۶ به Sea Club زنگ بزنم»). Without an API key the bot still runs:
messages become City Hall tasks and briefs use simple templates.

## Run it

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Get a Claude API key from https://platform.claude.com.
3. On your server:

```bash
git clone https://github.com/omidnmz/ocitytasks.git && cd ocitytasks
cp .env.example .env   # fill TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY
docker compose up -d --build
```

4. Send `/start` to your bot. The first chat that does becomes the owner (or set `OWNER_CHAT_ID`).

Without Docker: `pip install -r requirements.txt && python -m ocity`. The bot reads `.env`
from the folder it runs in, so this works the same on Windows:

```bat
cd C:\ocitytasks
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m ocity
```

All times are in `TIMEZONE` (default Asia/Tehran). Set `QUIET_WINDOWS=18:30-20:00`
to keep it silent during busy shop hours. Data lives in `data/ocity.db` (SQLite).

## Commands

| | |
|---|---|
| `/today` | morning brief now |
| `/tasks` | today's digest: overdue, today, staff, waiting, and one next step per building |
| `/all` | every open task by board |
| `/city` | buildings, floors, scaffolding |
| `/profile` | learned work patterns and your rules |
| `/plan` | import a content plan |
| `/sprint` | restart the 3-day content sprint |

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

`ocity/city.py` holds all the logic (no Telegram), `ocity/bot.py` is the Telegram layer,
`ocity/learning.py` builds the work profile, `ocity/ai.py` talks to Claude.

## Not in this version yet

- Voice messages (needs a speech-to-text service).
- The visual city map (will read the same database).
