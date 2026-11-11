"""The Mini App: a small web server inside the bot process.

Telegram opens `WEBAPP_URL` inside the chat. Every API call carries the
Mini App's signed `initData`; it is checked against the bot token and the
owner's id, so only the Mayor can read or change the city.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from .buildings import BUILDINGS
from .city import CityHall

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
MAX_AGE = 24 * 3600  # seconds a Mini App session stays valid
SNOOZE_OPTIONS = {"30", "60", "tonight", "tomorrow"}
REMIND_OPTIONS = {"1h", "evening", "tonight", "tomorrow", "week", "none"}


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


def make_app(city: CityHall, token: str, owner_id) -> web.Application:
    """owner_id: a function returning the owner's chat id (it can be claimed after startup)."""

    @web.middleware
    async def auth(request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        user = verify_init_data(request.headers.get("X-Init-Data", ""), token)
        if not user or user.get("id") != owner_id():
            return web.json_response({"error": "forbidden"}, status=403)
        city.seen(now_utc())
        return await handler(request)

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
            if task.status != "done":
                city.done(task_id, now)
            message = f"یه طبقه به {BUILDINGS[task.building].name} اضافه شد 🏗"
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

    app = web.Application(middlewares=[auth])
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/task", add_task)
    app.router.add_post("/api/task/{id:\\d+}/{action}", task_action)
    app.router.add_post("/api/building", add_building)
    app.router.add_post("/api/building/{key}", edit_building)
    return app


async def start(city: CityHall, token: str, owner_id, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(city, token, owner_id))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Mini App server on http://%s:%s", host, port)
    return runner
