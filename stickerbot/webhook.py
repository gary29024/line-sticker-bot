"""Webhook mode, used on Cloudflare.

Instead of the bot asking Telegram "any new messages?" over and over
(polling), Telegram sends each new message to the Cloudflare Worker, and the
Worker passes it to this small web server inside the container.

Addresses this server answers:
  POST /telegram  - a new message/button tap from Telegram
  GET  /health    - "are you alive, and are you busy converting?"
                    (the Worker asks this before putting the container to sleep)
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import signal
from collections import OrderedDict

from aiohttp import web
from telegram import Update
from telegram.ext import Application

from .bot import post_init

log = logging.getLogger(__name__)


def make_web_app(app: Application, secret: str) -> web.Application:
    # Telegram re-sends a message if it didn't get an answer in time (for example while
    # the container was waking up). Remember recent message numbers so each is handled once.
    seen: OrderedDict[int, None] = OrderedDict()

    async def telegram_update(request: web.Request) -> web.Response:
        given = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret and not hmac.compare_digest(given, secret):
            return web.Response(status=403, text="wrong secret")
        try:
            data = await request.json()
        except ValueError:
            return web.Response(status=400, text="not JSON")
        update_id = data.get("update_id") if isinstance(data, dict) else None
        if isinstance(update_id, int):
            if update_id in seen:
                return web.Response(text="duplicate")
            seen[update_id] = None
            while len(seen) > 2000:
                seen.popitem(last=False)
        await app.update_queue.put(Update.de_json(data, app.bot))
        return web.Response(text="ok")

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "busy": app.bot_data.get("active_jobs", 0)})

    web_app = web.Application(client_max_size=4 * 1024 * 1024)
    web_app.add_routes(
        [
            web.post("/telegram", telegram_update),
            web.get("/health", health),
            web.get("/", health),  # Cloudflare checks this to see that the bot has started
        ]
    )
    return web_app


async def serve(app: Application, port: int, secret: str) -> None:
    """Start the bot and the web server, and run until told to stop (SIGTERM)."""
    await app.initialize()
    await post_init(app)
    await app.start()

    runner = web.AppRunner(make_web_app(app, secret), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Webhook server listening on port %s", port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("Shutting down")
    await runner.cleanup()
    await app.stop()
    await app.shutdown()
    storage = app.bot_data.get("storage")
    if storage is not None:
        await storage.close()
