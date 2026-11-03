"""Building and editing sticker sets on Telegram.

Telegram rules this file follows:
  * A set made by a bot must have a short name ending in "_by_<bot username>",
    at most 64 characters, letters/digits/underscores, starting with a letter.
  * A new set is created with its first sticker; the rest are added one by one.
  * Telegram limits how fast bots may add stickers ("flood control"). When it
    says "slow down, retry in N seconds", we wait N seconds and try again.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import zipfile
from datetime import timedelta
from typing import Awaitable, Callable

from PIL import Image
from telegram import Bot, InputSticker, StickerSet
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

log = logging.getLogger(__name__)

NAME_MAX = 64
MAX_REGULAR = 120
MAX_EMOJI = 200

_TG_SET_LINK = re.compile(r"(?:t|telegram)\.me/(addstickers|addemoji)/([A-Za-z0-9_]{1,64})", re.I)


# --------------------------------------------------------------------------
# Names and links
# --------------------------------------------------------------------------


def make_set_name(prefix: str, pack_id: str, bot_username: str, copy: bool = False) -> str:
    """Make a valid Telegram set name, e.g. 'ls11537_by_mybot' or 'ls11537_a1b2c3_by_mybot'.

    prefix: 'ls' for LINE stickers, 'le' for LINE emoji.
    copy:   add a random tag so it doesn't clash with an earlier import of the same pack.
    """
    tail = f"_by_{bot_username}"
    suffix = f"_{secrets.token_hex(3)}" if copy else ""
    clean_id = re.sub(r"[^A-Za-z0-9]", "", pack_id)
    room = NAME_MAX - len(tail) - len(prefix) - len(suffix)
    if room < 1:
        raise ValueError("Bot username is too long to build a sticker set name")
    return f"{prefix}{clean_id[-room:]}{suffix}{tail}"


def parse_telegram_set_link(text: str) -> str | None:
    """'https://t.me/addstickers/Foo' -> 'Foo'."""
    match = _TG_SET_LINK.search(text)
    return match.group(2) if match else None


def set_limit(sticker_type: str) -> int:
    return MAX_EMOJI if sticker_type == "custom_emoji" else MAX_REGULAR


# --------------------------------------------------------------------------
# Talking to Telegram politely
# --------------------------------------------------------------------------


def _seconds(value: int | float | timedelta) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


async def call(fn: Callable[..., Awaitable], *args, attempts: int = 8, **kwargs):
    """Call a Telegram method, waiting and retrying when Telegram asks us to slow down
    or the network hiccups. "Bad request" errors are real mistakes and are not retried."""
    for attempt in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as exc:
            wait = _seconds(exc.retry_after) + 1
            log.info("Flood control: waiting %.0fs", wait)
            await asyncio.sleep(wait)
        except BadRequest:
            raise
        except (TimedOut, NetworkError):
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(min(30, 2**attempt))
    return await fn(*args, **kwargs)


async def get_set(bot: Bot, name: str) -> StickerSet | None:
    """The sticker set called `name`, or None if it doesn't exist."""
    try:
        return await call(bot.get_sticker_set, name)
    except BadRequest as exc:
        if "invalid" in str(exc).lower() or "not found" in str(exc).lower():
            return None
        raise


async def create_set(
    bot: Bot, user_id: int, name: str, title: str, first: InputSticker, sticker_type: str
) -> None:
    await call(
        bot.create_new_sticker_set,
        user_id=user_id,
        name=name,
        title=title[:NAME_MAX],
        stickers=[first],
        sticker_type=sticker_type,
        attempts=4,
    )


async def add_sticker(bot: Bot, user_id: int, name: str, sticker: InputSticker, count_before: int | None = None) -> None:
    """Add one sticker. If the upload times out we check whether it actually arrived
    before trying again, so the same sticker isn't added twice."""
    for attempt in range(8):
        try:
            await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker)
            return
        except RetryAfter as exc:
            await asyncio.sleep(_seconds(exc.retry_after) + 1)
        except BadRequest:
            raise
        except (TimedOut, NetworkError):
            if count_before is not None:
                current = await get_set(bot, name)
                if current is not None and len(current.stickers) > count_before:
                    return  # it went through after all
            if attempt == 7:
                raise
            await asyncio.sleep(min(30, 2**attempt))


# --------------------------------------------------------------------------
# Downloading a whole Telegram set as a ZIP
# --------------------------------------------------------------------------


def _extension(sticker) -> str:
    if sticker.is_animated:
        return "tgs"
    if sticker.is_video:
        return "webm"
    return "webp"


async def zip_set(
    bot: Bot, sticker_set: StickerSet, progress: Callable[[int, int], Awaitable[None]] | None = None
) -> bytes:
    """Download every sticker in a set into one ZIP. Still stickers get a PNG copy too."""
    stickers = list(sticker_set.stickers)
    results: list[tuple[int, str, bytes] | None] = [None] * len(stickers)
    gate = asyncio.Semaphore(4)
    done = 0

    async def fetch(i: int, sticker) -> None:
        nonlocal done
        async with gate:
            file = await call(bot.get_file, sticker.file_id)
            data = bytes(await call(file.download_as_bytearray))
        results[i] = (i, _extension(sticker), data)
        done += 1
        if progress:
            await progress(done, len(stickers))

    await asyncio.gather(*(fetch(i, s) for i, s in enumerate(stickers)))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in results:
            if item is None:
                continue
            i, ext, data = item
            zf.writestr(f"{sticker_set.name}/{i + 1:03d}.{ext}", data)
            if ext == "webp":
                try:
                    png = io.BytesIO()
                    Image.open(io.BytesIO(data)).save(png, "PNG")
                    zf.writestr(f"{sticker_set.name}/png/{i + 1:03d}.png", png.getvalue())
                except Exception:  # a damaged picture shouldn't spoil the whole ZIP
                    pass
    return buf.getvalue()
