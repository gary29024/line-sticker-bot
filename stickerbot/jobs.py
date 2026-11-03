"""The slow jobs: importing a LINE pack, and making ZIP downloads.

These run "in the background" so the bot can keep answering other people
while one pack is being converted. Each job keeps a single progress message
up to date ("Converting 12/40 ...") instead of spamming the chat.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import time
import zipfile
from dataclasses import dataclass, field

from telegram import Bot, InputFile, InputSticker, Message
from telegram.error import BadRequest, TelegramError

from . import convert, line_store, telegram_sets
from .storage import RemoteStorage, SqliteStorage, set_link

log = logging.getLogger(__name__)


class Progress:
    """Edits one chat message to show progress, at most once every few seconds."""

    def __init__(self, message: Message, every: float = 2.5):
        self.message = message
        self.every = every
        self._last_text = ""
        self._last_time = 0.0

    async def update(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if text == self._last_text or (not force and now - self._last_time < self.every):
            return
        self._last_text, self._last_time = text, now
        try:
            await self.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.debug("progress edit failed: %s", exc)
        except TelegramError as exc:
            log.debug("progress edit failed: %s", exc)


def bar(done: int, total: int, width: int = 12) -> str:
    filled = int(width * done / total) if total else width
    return "▓" * filled + "░" * (width - filled) + f" {done}/{total}"


# --------------------------------------------------------------------------
# Import a LINE pack into a new Telegram set
# --------------------------------------------------------------------------


@dataclass
class ImportRequest:
    user_id: int
    link: line_store.LineLink
    title: str
    set_name: str
    emojis: list[str]

    @property
    def sticker_type(self) -> str:
        return "custom_emoji" if self.link.kind == "emoji" else "regular"


@dataclass
class ImportResult:
    set_name: str
    link: str
    added: int = 0
    failed: list[str] = field(default_factory=list)
    title: str = ""


async def run_import(
    bot: Bot, storage: SqliteStorage | RemoteStorage, request: ImportRequest, progress: Progress, convert_workers: int = 2
) -> ImportResult:
    await progress.update("⬇️ Downloading from LINE…", force=True)
    pack = await line_store.download(request.link)
    emoji_mode = pack.kind == "emoji"
    total = len(pack.items)
    result = ImportResult(set_name=request.set_name, link=set_link(request.set_name, request.sticker_type), title=request.title)

    # Start converting everything (a couple at a time). Uploading starts as soon as
    # the first sticker is ready, so converting and uploading overlap.
    gate = asyncio.Semaphore(convert_workers)
    converted_count = 0

    async def convert_one(item: line_store.LineItem) -> convert.Converted:
        nonlocal converted_count
        async with gate:
            try:
                return await convert.convert(item.data, emoji=emoji_mode)
            finally:
                converted_count += 1

    tasks = [asyncio.create_task(convert_one(item)) for item in pack.items]
    created = False
    create_failures = 0
    try:
        for index, (item, task) in enumerate(zip(pack.items, tasks), start=1):
            try:
                sticker_file = await task
            except convert.ConvertError as exc:
                result.failed.append(f"#{index}: {exc}")
                continue
            sticker = InputSticker(
                sticker=InputFile(sticker_file.data, filename=sticker_file.filename),
                emoji_list=request.emojis,
                format=sticker_file.format,
            )
            try:
                if not created:
                    await telegram_sets.create_set(
                        bot, request.user_id, request.set_name, request.title, sticker, request.sticker_type
                    )
                    created = True
                    await storage.add_set(
                        request.set_name, request.title, request.user_id, request.sticker_type, request.link.store_url
                    )
                else:
                    await telegram_sets.add_sticker(
                        bot, request.user_id, request.set_name, sticker, count_before=result.added
                    )
                result.added += 1
            except BadRequest as exc:
                if not created and "occupied" in str(exc).lower():
                    raise  # name taken: the caller picks another name
                result.failed.append(f"#{index}: Telegram said “{exc.message}”")
                if not created:
                    create_failures += 1
                    if create_failures >= 3:
                        raise  # creating the set keeps failing - something bigger is wrong
            await progress.update(
                f"⚙️ Converting {bar(converted_count, total)}\n📤 Uploading {bar(result.added, total)}"
            )
    finally:
        for task in tasks:
            task.cancel()
    if not created:
        raise RuntimeError("None of the stickers could be converted:\n" + "\n".join(result.failed[:5]))
    return result


# --------------------------------------------------------------------------
# ZIP downloads
# --------------------------------------------------------------------------


async def run_line_zip(link: line_store.LineLink, progress: Progress) -> tuple[str, bytes, str]:
    """Download a LINE pack's original files (PNG / animated PNG, plus sounds) into a ZIP."""
    await progress.update("⬇️ Downloading from LINE…", force=True)
    pack = await line_store.download(link)
    folder = f"line_{pack.kind}_{link.pack_id}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, item in enumerate(pack.items, start=1):
            zf.writestr(f"{folder}/{n:02d}_{item.key}.png", item.data)
            if item.sound:
                zf.writestr(f"{folder}/{n:02d}_{item.key}.m4a", item.sound)
        zf.writestr(f"{folder}/info.txt", f"{pack.title}\n{link.store_url}\n{pack.describe()}\n")
    note = "Animated ones are animated PNG files - they play in web browsers." if pack.animated else ""
    return f"{folder}.zip", buf.getvalue(), f"📦 <b>{_esc(pack.title)}</b> — {pack.describe()}. {note}"


async def run_telegram_zip(bot: Bot, set_name: str, progress: Progress) -> tuple[str, bytes, str]:
    await progress.update("⬇️ Fetching sticker set…", force=True)
    sticker_set = await telegram_sets.get_set(bot, set_name)
    if sticker_set is None:
        raise RuntimeError("I can't find that Telegram sticker set.")

    async def step(done: int, total: int) -> None:
        await progress.update(f"⬇️ Downloading {bar(done, total)}")

    data = await telegram_sets.zip_set(bot, sticker_set, step)
    caption = f"📦 <b>{_esc(sticker_set.title)}</b> — {len(sticker_set.stickers)} stickers"
    return f"{sticker_set.name}.zip", data, caption


def _esc(text: str) -> str:
    return html.escape(text, quote=False)
