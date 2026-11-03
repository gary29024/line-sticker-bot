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
from .i18n import UserError, t
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
    lang: str = "en"  # the person's language, for progress messages

    @property
    def sticker_type(self) -> str:
        return "custom_emoji" if self.link.kind == "emoji" else "regular"


@dataclass
class ImportResult:
    set_name: str
    link: str
    added: int = 0
    failed: list[str] = field(default_factory=list)  # what went wrong, for the person to read
    failed_indices: list[int] = field(default_factory=list)  # which stickers (0 = first in the LINE pack)
    title: str = ""


async def run_import(
    bot: Bot, storage: SqliteStorage | RemoteStorage, request: ImportRequest, progress: Progress, convert_workers: int = 2
) -> ImportResult:
    lang = request.lang
    await progress.update(t(lang, "downloading_line"), force=True)
    pack = await line_store.download(request.link, lang=lang)
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
                result.failed.append(f"#{index}: {exc.text(lang)}")
                result.failed_indices.append(index - 1)
                continue
            sticker = InputSticker(
                # attach=True: a file inside a sticker must be sent as an attachment, or Telegram
                # receives an empty "sticker" field and refuses it.
                sticker=InputFile(sticker_file.data, filename=sticker_file.filename, attach=True),
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
                result.failed.append(f"#{index}: " + t(lang, "telegram_said", reason=exc.message))
                result.failed_indices.append(index - 1)
                if not created:
                    create_failures += 1
                    if create_failures >= 3:
                        raise  # creating the set keeps failing - something bigger is wrong
            await progress.update(
                t(lang, "progress.import", converted=bar(converted_count, total), uploaded=bar(result.added, total))
            )
    finally:
        for task in tasks:
            task.cancel()
    if not created:
        raise UserError("none_converted", details="\n".join(result.failed[:5]))
    return result


# --------------------------------------------------------------------------
# Retry the stickers that failed during an import
# --------------------------------------------------------------------------


@dataclass
class RetryRequest:
    owner_id: int
    set_name: str
    sticker_type: str
    link: line_store.LineLink
    indices: list[int]  # which stickers of the LINE pack are missing (0 = first)
    default_emoji: str
    lang: str = "en"


async def run_retry(bot: Bot, request: RetryRequest, progress: Progress) -> ImportResult:
    """Add the missing stickers of a LINE pack to its Telegram set, each in its original place."""
    lang = request.lang
    await progress.update(t(lang, "downloading_line"), force=True)
    pack = await line_store.download(request.link, lang=lang)
    indices = sorted(set(request.indices))
    if not indices or indices[-1] >= len(pack.items):
        raise UserError("retry_pack_changed")
    current = await telegram_sets.get_set(bot, request.set_name)
    if current is None:
        raise UserError("set_missing")
    if len(current.stickers) + len(indices) > len(pack.items):
        # The set already has (some of) them - e.g. the retry button was tapped twice.
        raise UserError("retry_nothing_missing")

    # Use the same emoji as the rest of the set.
    first_emoji = current.stickers[0].emoji if current.stickers else None
    emojis = [first_emoji] if first_emoji else [request.default_emoji]
    emoji_mode = request.sticker_type == "custom_emoji"
    result = ImportResult(
        set_name=request.set_name, link=set_link(request.set_name, request.sticker_type), title=current.title
    )

    still_missing = 0  # failed again; stickers after it sit one place earlier
    for done, index in enumerate(indices, start=1):
        await progress.update(t(lang, "progress.retry", bar=bar(done - 1, len(indices))))
        try:
            converted = await convert.convert(pack.items[index].data, emoji=emoji_mode)
        except convert.ConvertError as exc:
            result.failed.append(f"#{index + 1}: {exc.text(lang)}")
            result.failed_indices.append(index)
            still_missing += 1
            continue
        sticker = InputSticker(
            sticker=InputFile(converted.data, filename=converted.filename, attach=True),  # see run_import
            emoji_list=emojis,
            format=converted.format,
        )
        count_before = len(current.stickers)
        try:
            await telegram_sets.add_sticker(bot, request.owner_id, request.set_name, sticker, count_before)
            # Telegram puts new stickers at the end - move it back where it belongs.
            current = await telegram_sets.wait_for_count(bot, request.set_name, count_before + 1) or current
            target = min(index - still_missing, len(current.stickers) - 1)
            if len(current.stickers) > count_before and target < len(current.stickers) - 1:
                await telegram_sets.move_sticker(bot, current.stickers[-1].file_id, target)
            result.added += 1
        except BadRequest as exc:
            result.failed.append(f"#{index + 1}: " + t(lang, "telegram_said", reason=exc.message))
            result.failed_indices.append(index)
            still_missing += 1
    await progress.update(t(lang, "progress.retry", bar=bar(len(indices), len(indices))), force=True)
    return result


# --------------------------------------------------------------------------
# ZIP downloads
# --------------------------------------------------------------------------


async def run_line_zip(link: line_store.LineLink, progress: Progress, lang: str = "en") -> tuple[str, bytes, str]:
    """Download a LINE pack's original files (PNG / animated PNG, plus sounds) into a ZIP."""
    await progress.update(t(lang, "downloading_line"), force=True)
    pack = await line_store.download(link, lang=lang)
    folder = f"line_{pack.kind}_{link.pack_id}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, item in enumerate(pack.items, start=1):
            zf.writestr(f"{folder}/{n:02d}_{item.key}.png", item.data)
            if item.sound:
                zf.writestr(f"{folder}/{n:02d}_{item.key}.m4a", item.sound)
        zf.writestr(f"{folder}/info.txt", f"{pack.title}\n{link.store_url}\n{pack.describe(lang)}\n")
    note = t(lang, "line_zip_animated_note") if pack.animated else ""
    caption = t(lang, "line_zip_caption", title=_esc(pack.title), describe=_esc(pack.describe(lang)), note=note)
    return f"{folder}.zip", buf.getvalue(), caption.strip()


async def run_telegram_zip(bot: Bot, set_name: str, progress: Progress, lang: str = "en") -> tuple[str, bytes, str]:
    await progress.update(t(lang, "fetching_set"), force=True)
    sticker_set = await telegram_sets.get_set(bot, set_name)
    if sticker_set is None:
        raise UserError("set_not_found")

    async def step(done: int, total: int) -> None:
        await progress.update(t(lang, "progress.download", bar=bar(done, total)))

    data = await telegram_sets.zip_set(bot, sticker_set, step)
    caption = t(lang, "tg_zip_caption", title=_esc(sticker_set.title), count=len(sticker_set.stickers))
    return f"{sticker_set.name}.zip", data, caption


def _esc(text: str) -> str:
    return html.escape(text, quote=False)
