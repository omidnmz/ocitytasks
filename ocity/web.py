"""The Mini App: a small web server inside the bot process.

Telegram opens `WEBAPP_URL` inside the chat. Every API call carries the
Mini App's signed `initData`; it is checked against the bot token and the
owner's id, so only the Mayor can read or change the city.

The same page also works as a plain website (SITE_URL, e.g. https://city.modiret.ir). There is no
Telegram signature there, so the Mayor logs in with a one-time link the bot sends him in Telegram;
the browser then keeps a signed session cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from . import pipeline
from .buildings import BUILDINGS
from .city import CityHall

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
MAX_AGE = 24 * 3600  # seconds a Mini App session stays valid
SNOOZE_OPTIONS = {"30", "60", "tonight", "tomorrow"}
REMIND_OPTIONS = {"1h", "evening", "tonight", "tomorrow", "week", "none"}
COOKIE = "ocity_session"
SESSION_DAYS = 30
LINK_MINUTES = 10
LINK_EVERY = 60  # seconds between login links, so the button can't spam the Mayor


def _session_key(token: str, generation: str) -> bytes:
    return hmac.new(b"ocity-web-session", f"{token}:{generation}".encode(), hashlib.sha256).digest()


def make_session(token: str, generation: str, now: float | None = None) -> str:
    exp = str(int((now or time.time()) + SESSION_DAYS * 86400))
    sig = hmac.new(_session_key(token, generation), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_session(value: str, token: str, generation: str, now: float | None = None) -> bool:
    exp, _, sig = (value or "").partition(".")
    if not exp.isdigit() or not sig or not token:
        return False
    expected = hmac.new(_session_key(token, generation), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig) and int(exp) > (now or time.time())


def verify_init_data(init_data: str, token: str, now: float | None = None) -> dict | None:
    """Check Telegram's signature on the Mini App launch data; returns the user or None."""
    if not init_data or not token:
        return None
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if (now or time.time()) - int(fields.get("auth_date", "0")) > MAX_AGE:
            return None
        return json.loads(fields.get("user", "{}"))
    except (ValueError, TypeError):
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


LOGIN_PAGE = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ورود به شهر</title>
<style>body{font-family:system-ui,Tahoma,sans-serif;background:#f3f4fa;color:#1e1b4b;display:grid;place-items:center;
min-height:100vh;margin:0}form{background:#fff;padding:28px;border-radius:20px;text-align:center;max-width:320px;
box-shadow:0 10px 30px rgba(0,0,0,.08)}button{font:inherit;font-size:17px;border:0;border-radius:14px;padding:12px 28px;
background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;cursor:pointer}</style></head>
<body><form method="post"><p>🏙 ورود به شهر OCity روی این مرورگر</p><button>ورود</button></form></body></html>"""


def make_app(city: CityHall, token: str, owner_id, notify=None, site_url: str = "") -> web.Application:
    """owner_id: a function returning the owner's chat id (it can be claimed after startup).
    notify: an async function(text, url) that sends the Mayor a login link in Telegram;
    site_url: the website's own fixed address, which the link points at (never the request's Host)."""
    links: dict[str, float] = {}  # one-time login codes -> expiry
    last_link = [0.0]

    def generation() -> str:
        return city.db.get_state("web_session_gen") or "0"

    def cookie_ok(request: web.Request) -> bool:
        return check_session(request.cookies.get(COOKIE, ""), token, generation())

    @web.middleware
    async def auth(request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        user = verify_init_data(request.headers.get("X-Init-Data", ""), token)
        telegram = bool(user) and user.get("id") == owner_id()
        # a cookie alone is not enough: the custom header can't be sent by another site's form
        browser = request.headers.get("X-OCity") == "1" and cookie_ok(request)
        if not (telegram or browser):
            return web.json_response({"error": "forbidden", "login": bool(notify and site_url)}, status=403)
        city.seen(now_utc())
        return await handler(request)

    async def login_request(request: web.Request) -> web.Response:
        if not (notify and site_url and owner_id()):
            return web.json_response({"error": "off"}, status=404)
        now = time.time()
        if now - last_link[0] < LINK_EVERY:
            return web.json_response({"ok": True, "message": "لینک ورود همین الان فرستاده شد؛ توی تلگرام ببین."})
        last_link[0] = now
        for code, exp in list(links.items()):
            if exp < now:
                del links[code]
        code = secrets.token_urlsafe(32)
        links[code] = now + LINK_MINUTES * 60
        await notify(f"🔐 کسی (امیدوارم خودت!) خواست از مرورگر وارد شهر بشه.\n"
                     f"اگه خودت بودی دکمه زیر رو بزن؛ تا {LINK_MINUTES} دقیقه و فقط یه بار کار می‌کنه.\n"
                     f"اگه خودت نبودی، کاری نکن.", f"{site_url}/auth/{code}")
        return web.json_response({"ok": True, "message": "📩 لینک ورود رو توی تلگرام برات فرستادم."})

    async def login_page(request: web.Request) -> web.Response:
        # a page with a button, not an instant login, so link previews can't use up the code
        if links.get(request.match_info["code"], 0) < time.time():
            return web.Response(text="این لینک منقضی شده؛ دوباره «ورود» رو بزن.", status=410)
        return web.Response(text=LOGIN_PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def login_confirm(request: web.Request) -> web.Response:
        exp = links.pop(request.match_info["code"], 0)
        if exp < time.time():
            return web.Response(text="این لینک منقضی شده؛ دوباره «ورود» رو بزن.", status=410)
        response = web.Response(status=303, headers={"Location": "/"})
        response.set_cookie(COOKIE, make_session(token, generation()), max_age=SESSION_DAYS * 86400,
                            httponly=True, secure=site_url.startswith("https://"), samesite="Lax", path="/")
        return response

    async def logout(request: web.Request) -> web.Response:
        # signs out every browser: a new generation makes all old cookies invalid
        city.db.set_state("web_session_gen", secrets.token_hex(8))
        response = web.json_response({"ok": True, "message": "از همه مرورگرها خارج شدی."})
        response.del_cookie(COOKIE, path="/")
        return response

    async def index(request: web.Request) -> web.Response:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def state(request: web.Request) -> web.Response:
        data = city.city_state(now_utc())
        return web.json_response(data)

    async def task_action(request: web.Request) -> web.Response:
        task_id = int(request.match_info["id"])
        action = request.match_info["action"]
        task = city.db.get_task(task_id)
        if task is None:
            return web.json_response({"error": "not found"}, status=404)
        body = await request.json() if request.can_read_body else {}
        now = now_utc()
        message = ""
        if action == "done":
            message = f"یه طبقه به {BUILDINGS[task.building].name} اضافه شد 🏗"
            if task.status != "done":
                step = city.done(task_id, now).next_step
                if step:
                    message += f"\n🏛 {step}"
        elif action == "snooze" and body.get("option") in SNOOZE_OPTIONS:
            when = city.snooze(task_id, body["option"], now)
            message = f"⏰ دوباره می‌پرسم: {city.local(when):%H:%M}"
        elif action == "remind" and body.get("option") in REMIND_OPTIONS:
            message = "⏰ " + city.remind(task_id, body["option"], now)
        elif action == "staff":
            city.delegate(task_id, now)
            message = "👥 سپرده شد به پرسنل"
        elif action == "stuck":
            city.block(task_id, now)
            message = "🧱 رفت توی در انتظار"
        elif action == "drop":
            city.drop(task_id, now)
            message = "🗑 حذف شد"
        elif action == "building" and body.get("building") in BUILDINGS:
            city.set_building(task_id, body["building"])
            message = "🏷 ساختمونش عوض شد"
        else:
            return web.json_response({"error": "bad action"}, status=400)
        return web.json_response({"ok": True, "message": message})

    async def add_task(request: web.Request) -> web.Response:
        body = await request.json()
        text = str(body.get("text", "")).strip()[:500]
        if not text:
            return web.json_response({"error": "empty"}, status=400)
        now = now_utc()
        result = city.capture(text, now)
        task = result.added[0]
        if body.get("building") in BUILDINGS and task.building != "vault":
            city.set_building(task.id, body["building"])
        message = result.reply
        if body.get("when") in REMIND_OPTIONS:
            message = f"ثبت شد ✅\n⏰ {city.remind(task.id, body['when'], now)}"
        return web.json_response({"ok": True, "message": message, "id": task.id})

    async def add_building(request: web.Request) -> web.Response:
        body = await request.json()
        name = str(body.get("name", "")).strip()
        if not name:
            return web.json_response({"error": "empty"}, status=400)
        key = city.add_building(name, str(body.get("emoji") or "🏢"), str(body.get("district") or "tech"))
        if body.get("priority"):
            city.update_building(key, priority=True)
        b = BUILDINGS[key]
        return web.json_response({"ok": True, "key": key, "message": f"{b.emoji} {b.name} به شهر اضافه شد"})

    async def edit_building(request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if key not in BUILDINGS:
            return web.json_response({"error": "not found"}, status=404)
        body = await request.json()
        priority = body.get("priority")
        city.update_building(key, name=str(body.get("name") or ""), emoji=str(body.get("emoji") or ""),
                             priority=bool(priority) if priority is not None else None,
                             district=body.get("district"))
        return web.json_response({"ok": True, "message": "✏️ ذخیره شد"})

    async def add_plan(request: web.Request) -> web.Response:
        body = await request.json()
        text = str(body.get("text", ""))[:30000]
        if not pipeline.is_marketing_plan(text):
            return web.json_response({"error": "format", "message": "باید با «#برند: ...» شروع بشه."}, status=400)
        result = city.import_marketing_plan(text, now_utc())
        return web.json_response({"ok": True, "message": result.reply, "ask_times": result.ask_times})

    async def set_time(request: web.Request) -> web.Response:
        body = await request.json()
        kind, at = str(body.get("kind", "")), str(body.get("at", ""))
        if kind not in ("story", "filming", "editing", "publish") or not re.fullmatch(r"\d{2}:\d{2}", at):
            return web.json_response({"error": "bad time"}, status=400)
        city.set_kind_time(kind, at)
        return web.json_response({"ok": True, "message": f"⏰ ساعت {at} ثبت شد"})

    async def prompts(request: web.Request) -> web.Response:
        return web.json_response({"prompts": city.marketing_prompts(now_utc())})

    app = web.Application(middlewares=[auth])
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/task", add_task)
    app.router.add_post("/api/task/{id:\\d+}/{action}", task_action)
    app.router.add_post("/api/building", add_building)
    app.router.add_post("/api/plan", add_plan)
    app.router.add_post("/api/time", set_time)
    app.router.add_get("/api/prompts", prompts)
    app.router.add_post("/api/building/{key}", edit_building)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/auth/request", login_request)
    app.router.add_get("/auth/{code}", login_page)
    app.router.add_post("/auth/{code}", login_confirm)
    return app


async def start(city: CityHall, token: str, owner_id, host: str, port: int,
                notify=None, site_url: str = "") -> web.AppRunner:
    runner = web.AppRunner(make_app(city, token, owner_id, notify, site_url))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Mini App server on http://%s:%s", host, port)
    return runner
