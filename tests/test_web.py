import asyncio
import hashlib
import hmac
import json
import time
from datetime import time as dtime
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from ocity.city import CityHall
from ocity.config import Config
from ocity.db import DB
from ocity.web import make_app, verify_init_data

TOKEN = "123:abc"
OWNER = 42


def signed(user_id: int, token: str = TOKEN, auth_date: int | None = None) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_verify_init_data():
    assert verify_init_data(signed(OWNER), TOKEN)["id"] == OWNER
    assert verify_init_data(signed(OWNER, token="other"), TOKEN) is None
    assert verify_init_data(signed(OWNER, auth_date=int(time.time()) - 3 * 86400), TOKEN) is None
    assert verify_init_data("", TOKEN) is None


def run(coro_fn):
    city = CityHall(DB(":memory:"), Config(quiet_windows=[(dtime(18, 30), dtime(20, 0))]))

    async def main():
        client = TestClient(TestServer(make_app(city, TOKEN, lambda: OWNER)))
        await client.start_server()
        try:
            await coro_fn(client, city)
        finally:
            await client.close()

    asyncio.run(main())


def test_api_rejects_strangers_and_serves_the_owner():
    async def check(client, city):
        r = await client.get("/api/state", headers={"X-Init-Data": signed(7)})
        assert r.status == 403
        r = await client.get("/api/state", headers={"X-Init-Data": signed(OWNER)})
        data = await r.json()
        assert r.status == 200 and data["buildings"][0]["key"] == "media"
        r = await client.get("/")
        assert r.status == 200 and "OCity" in await r.text()
    run(check)


def test_api_adds_and_completes_a_task():
    async def check(client, city):
        h = {"X-Init-Data": signed(OWNER)}
        r = await client.post("/api/task", json={"text": "تدوین ریل", "when": "tomorrow"}, headers=h)
        task_id = (await r.json())["id"]
        task = city.db.get_task(task_id)
        assert task.building == "media" and task.due_time
        r = await client.post(f"/api/task/{task_id}/done", json={}, headers=h)
        assert r.status == 200 and city.db.get_task(task_id).status == "done"
        r = await client.post(f"/api/task/{task_id}/explode", json={}, headers=h)
        assert r.status == 400
        state = await (await client.get("/api/state", headers=h)).json()
        assert next(b for b in state["buildings"] if b["key"] == "media")["floors"] == 1
    run(check)


def test_api_renames_and_adds_buildings():
    async def check(client, city):
        h = {"X-Init-Data": signed(OWNER)}
        r = await client.post("/api/building/retail", json={"name": "کراکس شاپ", "emoji": "👟", "priority": True}, headers=h)
        assert r.status == 200
        r = await client.post("/api/building", json={"name": "باغ", "emoji": "🌳"}, headers=h)
        key = (await r.json())["key"]
        r = await client.post("/api/building/nope", json={"name": "x"}, headers=h)
        assert r.status == 404
        data = await (await client.get("/api/state", headers=h)).json()
        by = {b["key"]: b for b in data["buildings"]}
        assert by["retail"]["name"] == "کراکس شاپ" and by["retail"]["emoji"] == "👟" and by["retail"]["priority"]
        assert by[key]["name"] == "باغ" and by[key]["custom"] and not by["retail"]["custom"]
    run(check)
