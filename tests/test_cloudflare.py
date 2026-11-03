"""Checks for the Cloudflare set-up.

1. The webhook web server (what runs inside the Cloudflare Container).
2. RemoteStorage talking to the real D1 code in worker/store.ts, run locally by
   `wrangler dev` with a throwaway database. Skipped if Node/wrangler aren't installed
   (run `npm install` first).
"""

import os
import shutil
import socket
import subprocess
import time

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from telegram import Update

from stickerbot import bot as botmod
from stickerbot import webhook
from stickerbot.config import Config
from stickerbot.storage import RemoteStorage, SqliteStorage
from fake_telegram import FakeTelegram, UpdateMaker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- 1. Webhook server --------------------------------------------------------


@pytest.fixture
async def webhook_client(tmp_path):
    tg = FakeTelegram()
    config = Config(
        bot_token="123:TEST", data_dir=str(tmp_path), admin_ids=frozenset(), allowed_user_ids=frozenset(),
        default_emoji="⭐", max_jobs=2, convert_workers=2, log_level="INFO",
        mode="webhook", webhook_secret="s3cret-s3cret-s3cret",
    )
    app = botmod.build_app(config, SqliteStorage(config.db_path), request=tg)
    await app.initialize()
    await botmod.post_init(app)
    await app.start()
    client = TestClient(TestServer(webhook.make_web_app(app, config.webhook_secret)))
    await client.start_server()
    yield client, tg, app
    await client.close()
    await app.stop()
    await app.shutdown()


async def _wait(condition, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return
        await __import__("asyncio").sleep(0.05)
    raise AssertionError("timed out")


async def test_webhook_accepts_telegram_updates(webhook_client):
    client, tg, app = webhook_client
    update = UpdateMaker(user_id=42).text("/start")
    headers = {"X-Telegram-Bot-Api-Secret-Token": "s3cret-s3cret-s3cret"}

    resp = await client.post("/telegram", json=update, headers=headers)
    assert resp.status == 200
    await _wait(lambda: any("LINE stickers into Telegram" in t for t in tg.texts()))

    # The same message delivered twice (Telegram retry) is only handled once.
    before = len(tg.called("sendMessage"))
    resp = await client.post("/telegram", json=update, headers=headers)
    assert await resp.text() == "duplicate"
    await __import__("asyncio").sleep(0.3)
    assert len(tg.called("sendMessage")) == before


async def test_webhook_rejects_wrong_secret(webhook_client):
    client, tg, _ = webhook_client
    resp = await client.post("/telegram", json=UpdateMaker().text("/start"), headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert resp.status == 403
    resp = await client.post("/telegram", json=UpdateMaker().text("/start"))
    assert resp.status == 403


async def test_health_reports_busy_jobs(webhook_client):
    client, _, app = webhook_client
    assert (await (await client.get("/health")).json()) == {"ok": True, "busy": 0}
    app.bot_data["active_jobs"] = 1
    assert (await (await client.get("/health")).json())["busy"] == 1
    assert (await client.get("/")).status == 200  # Cloudflare's start-up check


# ---- 2. Storage: local file and Cloudflare D1 behave the same ---------------------


async def storage_scenario(db):
    rec = await db.add_set("ls1_by_bot", "Title", 42, "regular", "https://line")
    assert rec.name == "ls1_by_bot" and rec.owner_id == 42 and rec.sticker_type == "regular"
    assert (await db.get_by_name("LS1_BY_BOT")).id == rec.id  # names are case-insensitive
    assert (await db.get_by_id(rec.id)).title == "Title"
    assert await db.get_by_name("missing") is None
    assert await db.get_by_id(999999) is None

    big_id = 7_000_000_123  # Telegram user IDs can be bigger than 32 bits
    await db.add_set("le2_by_bot", "Emoji", big_id, "custom_emoji", "")
    assert [r.name for r in await db.list_for_owner(big_id)] == ["le2_by_bot"]
    assert [r.name for r in await db.list_for_owner(42)] == ["ls1_by_bot"]

    await db.rename("ls1_by_bot", "New “title” 🐻")
    assert (await db.get_by_id(rec.id)).title == "New “title” 🐻"
    again = await db.add_set("ls1_by_bot", "Again", 43, "regular", "x")  # same name = update, not duplicate
    assert again.id == rec.id and again.owner_id == 43

    await db.delete("ls1_by_bot")
    assert await db.get_by_name("ls1_by_bot") is None
    assert await db.list_for_owner(43) == []


async def test_sqlite_storage(tmp_path):
    await storage_scenario(SqliteStorage(str(tmp_path / "db.sqlite3")))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_wrangler_dev(tmp_path, config: str, extra_args=(), probe="/"):
    """Start `wrangler dev` for a test config on a free local port; yields its address."""
    wrangler = os.path.join(ROOT, "node_modules", ".bin", "wrangler")
    if not os.path.exists(wrangler) or shutil.which("node") is None:
        pytest.skip("run `npm install` to test the Cloudflare code")
    port = _free_port()
    env = dict(os.environ, WRANGLER_SEND_METRICS="false", CI="1")
    proc = subprocess.Popen(
        [wrangler, "dev", "-c", os.path.join(ROOT, "tests", config), "--ip", "127.0.0.1", "--port", str(port),
         "--persist-to", str(tmp_path / "wrangler-state"), *extra_args],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail("wrangler dev exited:\n" + proc.stdout.read())
            try:
                httpx.get(url + probe, timeout=2, trust_env=False)
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.fail("wrangler dev didn't start")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def store_worker(tmp_path):
    yield from run_wrangler_dev(tmp_path, "store_worker/wrangler.jsonc")


SECRET = "test_secret_0123456789abcdef"


@pytest.fixture
def front_desk(tmp_path):
    yield from run_wrangler_dev(
        tmp_path, "front_desk/wrangler.jsonc", ["--var", "BOT_TOKEN:123:TEST", "--var", f"WEBHOOK_SECRET:{SECRET}"]
    )


def test_worker_front_desk(front_desk):
    get = lambda path, **kw: httpx.get(front_desk + path, trust_env=False, timeout=10, **kw)
    post = lambda path, **kw: httpx.post(front_desk + path, trust_env=False, timeout=10, **kw)

    home = get("/")
    assert home.status_code == 200 and "sticker bot" in home.text
    assert get("/nope").status_code == 404

    # Setup page needs the secret key
    assert get("/setup").status_code == 403
    assert get("/setup?key=wrong").status_code == 403

    # Messages without Telegram's secret password are refused before reaching the bot
    assert get("/telegram").status_code == 405
    assert post("/telegram", json={"update_id": 1}).status_code == 403
    assert post("/telegram", json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": SECRET + "x"}).status_code == 403

    # With the right password but no container available (switched off in this test), the Worker
    # must not fail silently: it answers Telegram "OK" (so Telegram stops re-sending) and tries to
    # tell the person the bot is starting up.
    update = {"update_id": 5, "message": {"message_id": 1, "date": 0, "chat": {"id": 42, "type": "private"},
                                          "text": "/start"}}
    resp = httpx.post(front_desk + "/telegram", json=update, trust_env=False, timeout=60,
                      headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert resp.status_code == 200 and "user notified" in resp.text

    # Status page needs the key, and reports on the container
    assert get("/status").status_code == 403
    status = httpx.get(front_desk + f"/status?key={SECRET}", trust_env=False, timeout=60)
    assert status.status_code == 200 and "Container:" in status.text and "Wake the bot now" in status.text


async def test_cloudflare_d1_storage(store_worker):
    db = RemoteStorage(store_worker)
    try:
        await storage_scenario(db)
        # Bad requests are refused rather than written to the database.
        bad = await db.client.post(store_worker + "/add_set", json={"name": "", "title": "x"})
        assert bad.status_code == 400
        unknown = await db.client.post(store_worker + "/drop_table", json={})
        assert unknown.status_code == 400
    finally:
        await db.close()


async def test_import_through_webhook_with_d1_storage(store_worker, tmp_path, monkeypatch):
    """A whole conversation the way it runs on Cloudflare: messages arrive at the webhook
    server, and the owner list is kept by the real D1 code."""
    from stickerbot import line_store
    from test_conversation import LINK, fake_pack

    async def fake_info(link, client=None):
        pack = fake_pack(link)
        pack.items = []
        return pack

    async def fake_download(link, client=None):
        return fake_pack(link)

    monkeypatch.setattr(line_store, "fetch_info", fake_info)
    monkeypatch.setattr(line_store, "download", fake_download)

    tg = FakeTelegram()
    config = Config(
        bot_token="123:TEST", data_dir=str(tmp_path), admin_ids=frozenset(), allowed_user_ids=frozenset(),
        default_emoji="⭐", max_jobs=2, convert_workers=2, log_level="INFO",
        mode="webhook", webhook_secret=SECRET, store_url=store_worker,
    )
    app = botmod.build_app(config, request=tg)
    assert isinstance(app.bot_data["storage"], RemoteStorage)
    await app.initialize()
    await botmod.post_init(app)
    await app.start()
    client = TestClient(TestServer(webhook.make_web_app(app, SECRET)))
    await client.start_server()
    user = UpdateMaker(user_id=42)
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    try:
        for update in (user.text(LINK), user.button("line:make"), user.button("imp:title"), user.button("imp:emoji")):
            assert (await client.post("/telegram", json=update, headers=headers)).status == 200
            await __import__("asyncio").sleep(0.2)
        await _wait(lambda: any("Done!" in t for t in tg.texts()), timeout=60)
        assert (await (await client.get("/health")).json())["busy"] == 0  # job finished, safe to sleep

        # The owner list really is in D1: ask the Worker directly
        stored = httpx.post(store_worker + "/list_for_owner", json={"owner_id": 42}, trust_env=False).json()
        assert [r["name"] for r in stored["result"]] == ["ls11537_by_test_sticker_bot"]

        # ...and /manage reads it back from there
        await client.post("/telegram", json=user.text("/manage"), headers=headers)
        await _wait(lambda: "Your sets" in tg.texts()[-1])
        assert "Brown" in tg.last_markup()[0][0]["text"]
    finally:
        await client.close()
        await app.stop()
        await app.shutdown()
        await app.bot_data["storage"].close()
