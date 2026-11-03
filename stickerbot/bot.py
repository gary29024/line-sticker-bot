"""The chat part of the bot: what it says, which buttons it shows, and what
happens when you tap a button or send it something.

How a conversation works, in plain words:
  * The bot remembers, per person, what it's waiting for (a set name, an emoji,
    pictures to add...). That note is kept in `context.user_data["waiting"]`.
  * Every message you send is looked at in light of that note.
  * Buttons carry a short code (like "set:rename") that tells the bot which
    button was tapped.
  * Everything the bot says comes from i18n.py, in the person's language
    (English or Traditional Chinese). T(context, "key") picks the right one.
"""

from __future__ import annotations

import asyncio
import html
import logging

import emoji as emoji_lib
from telegram import (
    BotCommand,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    InputFile,
    InputSticker,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import BaseRequest

from . import convert, i18n, jobs, line_store, telegram_sets
from .config import Config
from .i18n import UserError
from .storage import RemoteStorage, SetRecord, SqliteStorage, set_link

log = logging.getLogger(__name__)

MAX_BOT_DOWNLOAD = 20 * 1024 * 1024  # Telegram lets bots download files up to 20 MB
MAX_BOT_UPLOAD = 49 * 1024 * 1024  # ...and send files up to 50 MB


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def kb(*rows: list[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])


def L(context: ContextTypes.DEFAULT_TYPE) -> str:
    """This person's language: "en" or "zh" (worked out by the gatekeeper below)."""
    return context.user_data.get("lang", "en")


def T(context: ContextTypes.DEFAULT_TYPE, key: str, **params) -> str:
    """A message in this person's language."""
    return i18n.t(L(context), key, **params)


def main_menu(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    return kb(
        [Btn(T(context, "btn.import"), callback_data="menu:import")],
        [
            Btn(T(context, "btn.my_sets"), callback_data="menu:manage"),
            Btn(T(context, "btn.download_zip"), callback_data="menu:download"),
        ],
    )


def done_button(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    return kb([Btn(T(context, "btn.done"), callback_data="add:done")])


def cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.bot_data["config"]


def store(context: ContextTypes.DEFAULT_TYPE) -> SqliteStorage | RemoteStorage:
    return context.bot_data["storage"]


def parse_emojis(text: str) -> list[str]:
    """All emoji in a piece of text, without repeats (Telegram allows up to 20 per sticker)."""
    found: list[str] = []
    for match in emoji_lib.emoji_list(text or ""):
        if match["emoji"] not in found:
            found.append(match["emoji"])
    return found[:20]


def reset(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forget whatever the bot was waiting for from this person."""
    for key in ("waiting", "import", "sticker"):
        context.user_data.pop(key, None)


async def say(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup=None):
    return await context.bot.send_message(
        update.effective_chat.id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )


async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, markup=None) -> None:
    try:
        await context.bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )
    except (Forbidden, TelegramError) as exc:
        log.warning("Couldn't message %s: %s", chat_id, exc)


async def owned_set(context: ContextTypes.DEFAULT_TYPE, user_id: int, *, set_id=None, name=None) -> SetRecord | None:
    """The set, but only if this person may edit it (they made it, or they're an admin)."""
    if set_id is not None:
        record = await store(context).get_by_id(int(set_id))
    elif name:
        record = await store(context).get_by_name(name)
    else:
        return None
    if record and (record.owner_id == user_id or user_id in cfg(context).admin_ids):
        return record
    return None


def set_name_for(link: line_store.LineLink, bot_username: str, copy: bool = False) -> str:
    prefix = "le" if link.kind == "emoji" else "ls"
    return telegram_sets.make_set_name(prefix, link.pack_id, bot_username, copy=copy)


async def download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    file = await telegram_sets.call(context.bot.get_file, file_id)
    return bytes(await file.download_as_bytearray())


def noun(context: ContextTypes.DEFAULT_TYPE, sticker_type: str) -> str:
    return T(context, "noun.emoji" if sticker_type == "custom_emoji" else "noun.stickers")


def background(context: ContextTypes.DEFAULT_TYPE, chat_id: int, work, start_text: str | None = None):
    """Run a slow job without freezing the bot. Only one job per person at a time;
    across everyone, at most MAX_JOBS run together (the rest wait their turn)."""
    user_data = context.user_data
    user_data["busy"] = True
    lang = L(context)
    slots: asyncio.Semaphore = context.bot_data["jobs"]
    bot_data = context.bot_data
    # Counted so the Cloudflare host knows not to put the bot to sleep mid-job.
    bot_data["active_jobs"] = bot_data.get("active_jobs", 0) + 1

    async def runner():
        try:
            text = i18n.t(lang, "busy_wait") if slots.locked() else (start_text or i18n.t(lang, "getting_started"))
            message = await context.bot.send_message(chat_id, text)
            async with slots:
                await work(jobs.Progress(message))
        except UserError as exc:
            await safe_send(context, chat_id, f"😕 {esc(exc.text(lang))}")
        except BadRequest as exc:
            log.warning("Job failed: %s", exc)
            await safe_send(context, chat_id, i18n.t(lang, "err.telegram_refused", reason=esc(exc.message)))
        except Exception as exc:  # report anything unexpected instead of failing silently
            log.exception("Job failed")
            await safe_send(context, chat_id, i18n.t(lang, "err.failed", reason=esc(str(exc))[:400]))
        finally:
            user_data["busy"] = False
            bot_data["active_jobs"] = max(0, bot_data.get("active_jobs", 1) - 1)

    return context.application.create_task(runner())


async def ensure_not_busy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("busy"):
        await say(update, context, T(context, "still_busy"))
        return False
    return True


# --------------------------------------------------------------------------
# Who may use the bot, and in which language
# --------------------------------------------------------------------------


async def resolve_language(context: ContextTypes.DEFAULT_TYPE, user) -> str:
    """The person's chosen language (/language), or else their Telegram app's language.
    Looked up once and then remembered while the bot is running."""
    ud = context.user_data
    if ud.get("lang") in i18n.LANGS:
        return ud["lang"]
    saved = None
    try:
        saved = await store(context).get_lang(user.id)
    except Exception as exc:  # a database hiccup shouldn't stop the bot from answering
        log.warning("Couldn't look up language for %s: %s", user.id, exc)
    ud["lang"] = saved if saved in i18n.LANGS else i18n.lang_from_telegram(user.language_code)
    return ud["lang"]


async def gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before everything else. Ignores group chats and, if ALLOWED_USER_IDS is set, strangers.
    Also works out which language to speak."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or chat.type != ChatType.PRIVATE:
        raise ApplicationHandlerStop
    if not cfg(context).is_allowed(user.id):
        lang = i18n.lang_from_telegram(user.language_code)
        if update.callback_query:
            await update.callback_query.answer(i18n.t(lang, "private_bot_alert"), show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(i18n.t(lang, "private_bot", user_id=user.id))
        raise ApplicationHandlerStop
    await resolve_language(context, user)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    await say(update, context, T(context, "help"), main_menu(context))


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    context.user_data["waiting"] = "import_link"
    await say(update, context, T(context, "import_prompt"))


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    context.user_data["waiting"] = "download_link"
    await say(update, context, T(context, "download_prompt"))


async def cmd_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    sets = await store(context).list_for_owner(update.effective_user.id)
    if not sets:
        await say(update, context, T(context, "no_sets"))
        return
    rows = [
        [Btn(("😀 " if s.sticker_type == "custom_emoji" else "🖼 ") + s.title[:45], callback_data=f"open:{s.id}")]
        for s in sets[:60]
    ]
    await say(update, context, T(context, "your_sets"), kb(*rows))


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await say(
        update,
        context,
        T(context, "lang_prompt"),
        kb(
            [
                Btn(T(context, "btn.lang_zh"), callback_data="lang:zh"),
                Btn(T(context, "btn.lang_en"), callback_data="lang:en"),
            ],
            [Btn(T(context, "btn.lang_auto"), callback_data="lang:auto")],
        ),
    )


async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str) -> None:
    user = update.effective_user
    if choice == "auto":
        await store(context).set_lang(user.id, None)
        context.user_data["lang"] = i18n.lang_from_telegram(user.language_code)
        await say(update, context, T(context, "lang_auto"))
    else:
        await store(context).set_lang(user.id, choice)
        context.user_data["lang"] = choice
        await say(update, context, T(context, "lang_set"))
    await cmd_start(update, context)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await say(update, context, T(context, "myid", user_id=update.effective_user.id))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    await say(update, context, T(context, "cancelled"))


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    was_adding = context.user_data.get("waiting") == "add"
    reset(context)
    record = await owned_set(context, update.effective_user.id, name=context.user_data.get("set_name"))
    if was_adding and record:
        await show_set_menu(update, context, record)
    else:
        await say(update, context, T(context, "all_done"))


# --------------------------------------------------------------------------
# Text messages
# --------------------------------------------------------------------------


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    waiting = context.user_data.get("waiting")
    handlers = {
        "title": got_import_title,
        "emoji": got_import_emoji,
        "rename": got_rename,
        "move": got_move,
        "sticker_emoji": got_sticker_emoji,
    }
    if waiting in handlers:
        await handlers[waiting](update, context, text)
        return

    link = line_store.parse_line_link(text)
    if link:
        await got_line_link(update, context, link)
        return
    tg_name = telegram_sets.parse_telegram_set_link(text)
    if tg_name:
        await got_telegram_set(update, context, tg_name)
        return
    if waiting == "add":
        await say(update, context, T(context, "add_hint"), done_button(context))
        return
    await say(update, context, T(context, "didnt_understand"))


# --------------------------------------------------------------------------
# LINE links: import or download
# --------------------------------------------------------------------------


async def got_line_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: line_store.LineLink) -> None:
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        pack = await line_store.fetch_info(link, lang=L(context))
    except line_store.LineError as exc:
        await say(update, context, f"😕 {esc(exc.text(L(context)))}")
        return
    ud = context.user_data
    mode = ud.get("waiting")
    reset(context)
    ud["line"] = {"link": link, "title": pack.title, "kind": pack.kind}

    if mode == "import_link":
        await start_import(update, context)
        return
    if mode == "download_link":
        await start_line_zip(update, context)
        return
    note = T(context, "emoji_note") if pack.kind == "emoji" else ""
    await say(
        update,
        context,
        T(context, "found_pack", title=esc(pack.title), describe=esc(pack.describe(L(context))), note=note),
        kb(
            [Btn(T(context, "btn.make_set"), callback_data="line:make")],
            [Btn(T(context, "btn.download_originals"), callback_data="line:zip")],
        ),
    )


async def start_import(update: Update, context: ContextTypes.DEFAULT_TYPE, copy: bool = False) -> None:
    ud = context.user_data
    line = ud.get("line")
    if not line:
        await say(update, context, T(context, "send_link_first"))
        return
    if not await ensure_not_busy(update, context):
        return
    link: line_store.LineLink = line["link"]
    name = set_name_for(link, context.bot.username, copy=copy)

    if not copy:
        existing = await telegram_sets.get_set(context.bot, name)
        if existing is not None:
            record = await store(context).get_by_name(name)
            url = set_link(existing.name, existing.sticker_type)
            mine = record and record.owner_id == update.effective_user.id
            text = T(context, "already_yours" if mine else "already_converted", url=url, title=esc(existing.title))
            await say(update, context, text, kb(
                [Btn(T(context, "btn.open_set"), url=url)],
                [Btn(T(context, "btn.own_copy"), callback_data="imp:copy")],
            ))
            return

    ud["import"] = {"link": link, "set_name": name, "title": line["title"][: telegram_sets.NAME_MAX]}
    ud["waiting"] = "title"
    await say(
        update,
        context,
        T(context, "ask_title", title=esc(ud["import"]["title"])),
        kb(
            [Btn(T(context, "btn.keep_name"), callback_data="imp:title")],
            [Btn(T(context, "btn.cancel"), callback_data="imp:cancel")],
        ),
    )


async def got_import_title(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    title = " ".join(text.split())[: telegram_sets.NAME_MAX]
    if not title or "import" not in context.user_data:
        await say(update, context, T(context, "type_name"))
        return
    context.user_data["import"]["title"] = title
    await ask_import_emoji(update, context)


async def ask_import_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["waiting"] = "emoji"
    await say(
        update,
        context,
        T(context, "ask_emoji"),
        kb(
            [Btn(T(context, "btn.use_default_emoji", emoji=cfg(context).default_emoji), callback_data="imp:emoji")],
            [Btn(T(context, "btn.cancel"), callback_data="imp:cancel")],
        ),
    )


async def got_import_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    emojis = parse_emojis(text)
    if not emojis:
        await say(update, context, T(context, "not_emoji"))
        return
    await launch_import(update, context, emojis)


async def launch_import(update: Update, context: ContextTypes.DEFAULT_TYPE, emojis: list[str]) -> None:
    ud = context.user_data
    plan = ud.get("import")
    if not plan:
        await say(update, context, T(context, "lost"))
        return
    if not await ensure_not_busy(update, context):
        return
    reset(context)
    user_id, chat_id, lang = update.effective_user.id, update.effective_chat.id, L(context)
    request = jobs.ImportRequest(
        user_id=user_id, link=plan["link"], title=plan["title"], set_name=plan["set_name"], emojis=emojis, lang=lang
    )
    bot, storage, workers = context.bot, store(context), cfg(context).convert_workers

    async def work(progress: jobs.Progress) -> None:
        try:
            result = await jobs.run_import(bot, storage, request, progress, workers)
        except BadRequest as exc:
            if "occupied" not in str(exc).lower():
                raise
            # Someone grabbed this name a moment ago – use a unique one instead.
            request.set_name = set_name_for(request.link, bot.username, copy=True)
            result = await jobs.run_import(bot, storage, request, progress, workers)

        record = await storage.get_by_name(result.set_name)
        text = i18n.t(
            lang, "import_done", title=esc(result.title), count=result.added,
            noun=i18n.t(lang, "noun.emoji" if request.sticker_type == "custom_emoji" else "noun.stickers"),
            link=result.link,
        )
        if result.failed:
            details = "\n".join(esc(f) for f in result.failed[:8])
            text += i18n.t(lang, "import_some_failed", count=len(result.failed), details=details)
        await progress.update(i18n.t(lang, "finished"), force=True)
        rows = [[Btn(i18n.t(lang, "btn.add_to_telegram"), url=result.link)]]
        if record:
            rows.append([Btn(i18n.t(lang, "btn.manage_this_set"), callback_data=f"open:{record.id}")])
        await safe_send(context, chat_id, text, kb(*rows))

    background(context, chat_id, work, T(context, "downloading_line"))


async def start_line_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    line = context.user_data.get("line")
    if not line:
        await say(update, context, T(context, "send_link_first"))
        return
    if not await ensure_not_busy(update, context):
        return
    chat_id, lang = update.effective_chat.id, L(context)
    link = line["link"]

    async def work(progress: jobs.Progress) -> None:
        filename, data, caption = await jobs.run_line_zip(link, progress, lang)
        await send_zip(context, chat_id, progress, filename, data, caption, lang)

    background(context, chat_id, work, T(context, "downloading_line"))


async def send_zip(context, chat_id: int, progress: jobs.Progress, filename: str, data: bytes, caption: str,
                   lang: str) -> None:
    if len(data) > MAX_BOT_UPLOAD:
        raise UserError("zip_too_big")
    await progress.update(i18n.t(lang, "sending_zip"), force=True)
    await telegram_sets.call(
        context.bot.send_document,
        chat_id,
        InputFile(data, filename=filename),
        caption=caption,
        parse_mode=ParseMode.HTML,
        attempts=3,
    )
    await progress.update(i18n.t(lang, "finished"), force=True)


# --------------------------------------------------------------------------
# Telegram sets: download
# --------------------------------------------------------------------------


async def got_telegram_set(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    mode = context.user_data.get("waiting")
    reset(context)
    context.user_data["tg_set"] = name
    if mode == "download_link":
        await start_telegram_zip(update, context)
        return
    sticker_set = await telegram_sets.get_set(context.bot, name)
    if sticker_set is None:
        await say(update, context, "😕 " + T(context, "set_not_found"))
        return
    rows = [[Btn(T(context, "btn.download_as_zip"), callback_data="tg:zip")]]
    record = await owned_set(context, update.effective_user.id, name=name)
    if record:
        rows.append([Btn(T(context, "btn.manage_this_set"), callback_data=f"open:{record.id}")])
    await say(
        update,
        context,
        T(context, "tg_set_info", title=esc(sticker_set.title), count=len(sticker_set.stickers)),
        kb(*rows),
    )


async def start_telegram_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = context.user_data.get("tg_set")
    if not name:
        await say(update, context, T(context, "send_set_first"))
        return
    if not await ensure_not_busy(update, context):
        return
    chat_id, lang = update.effective_chat.id, L(context)
    bot = context.bot

    async def work(progress: jobs.Progress) -> None:
        filename, data, caption = await jobs.run_telegram_zip(bot, name, progress, lang)
        await send_zip(context, chat_id, progress, filename, data, caption, lang)

    background(context, chat_id, work, T(context, "fetching_set"))


# --------------------------------------------------------------------------
# Managing a set
# --------------------------------------------------------------------------


async def show_set_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, record: SetRecord) -> None:
    sticker_set = await telegram_sets.get_set(context.bot, record.name)
    if sticker_set is None:
        await store(context).delete(record.name)
        await say(update, context, T(context, "set_gone"))
        return
    context.user_data.pop("waiting", None)
    context.user_data["set_name"] = record.name
    await say(
        update,
        context,
        T(
            context, "set_menu", title=esc(sticker_set.title), link=record.link, count=len(sticker_set.stickers),
            limit=telegram_sets.set_limit(record.sticker_type), noun=noun(context, record.sticker_type),
        ),
        kb(
            [
                Btn(T(context, "btn.add_stickers"), callback_data="set:add"),
                Btn(T(context, "btn.rename"), callback_data="set:rename"),
            ],
            [
                Btn(T(context, "btn.download_zip"), callback_data="set:zip"),
                Btn(T(context, "btn.delete_set"), callback_data="set:delete"),
            ],
            [Btn(T(context, "btn.back_sets"), callback_data="menu:manage")],
        ),
    )


async def current_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> SetRecord | None:
    return await owned_set(context, update.effective_user.id, name=context.user_data.get("set_name"))


async def got_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    record = await current_set(update, context)
    title = " ".join(text.split())[: telegram_sets.NAME_MAX]
    if not record or not title:
        reset(context)
        await say(update, context, T(context, "pick_set_first"))
        return
    await telegram_sets.call(context.bot.set_sticker_set_title, record.name, title)
    await store(context).rename(record.name, title)
    reset(context)
    await say(update, context, T(context, "renamed", title=esc(title)))
    await show_set_menu(update, context, await store(context).get_by_name(record.name))


async def add_to_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add whatever the person just sent (picture, GIF, video, sticker) to the set they're editing."""
    msg = update.effective_message
    record = await current_set(update, context)
    if record is None:
        reset(context)
        await say(update, context, T(context, "pick_set_first"))
        return
    lock = context.user_data.setdefault("add_lock", asyncio.Lock())
    async with lock:  # one at a time, so a batch of photos keeps its order
        emoji_mode = record.sticker_type == "custom_emoji"
        emojis = parse_emojis(msg.caption or "")
        sticker_input = None
        file_obj = None

        if msg.sticker:
            s = msg.sticker
            emojis = emojis or parse_emojis(s.emoji or "")
            if not emoji_mode and s.type == "regular":
                # Already a Telegram sticker of the right kind: reuse it as-is, no converting needed.
                fmt = "animated" if s.is_animated else "video" if s.is_video else "static"
                sticker_input = InputSticker(s.file_id, emojis or [cfg(context).default_emoji], fmt)
            elif s.is_animated:
                await msg.reply_text(T(context, "tgs_no_emoji"))
                return
            file_obj = s
        elif msg.photo:
            file_obj = msg.photo[-1]
        elif msg.animation:
            file_obj = msg.animation
        elif msg.video:
            file_obj = msg.video
        elif msg.document:
            file_obj = msg.document
        if file_obj is None:
            await msg.reply_text(T(context, "send_media"))
            return
        if (file_obj.file_size or 0) > MAX_BOT_DOWNLOAD:
            await msg.reply_text(T(context, "file_too_big"))
            return

        sticker_set = await telegram_sets.get_set(context.bot, record.name)
        if sticker_set is None:
            await msg.reply_text(T(context, "set_missing"))
            return
        count = len(sticker_set.stickers)
        if count >= telegram_sets.set_limit(record.sticker_type):
            await msg.reply_text(T(context, "set_full"))
            return

        if sticker_input is None:
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.CHOOSE_STICKER)
            data = await download_telegram_file(context, file_obj.file_id)
            try:
                converted = await convert.convert(data, emoji=emoji_mode)
            except convert.ConvertError as exc:
                await msg.reply_text(f"😕 {exc.text(L(context))}")
                return
            sticker_input = InputSticker(
                InputFile(converted.data, filename=converted.filename, attach=True),  # see jobs.py
                emojis or [cfg(context).default_emoji],
                converted.format,
            )
        try:
            await telegram_sets.add_sticker(context.bot, record.owner_id, record.name, sticker_input, count)
        except BadRequest as exc:
            await msg.reply_text(T(context, "err.telegram_refused", reason=exc.message))
            return
        await msg.reply_text(T(context, "added", count=count + 1), reply_markup=done_button(context))


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("waiting") == "add":
        await add_to_set(update, context)
        return
    await say(update, context, T(context, "media_how"))


async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud = context.user_data
    sticker = update.effective_message.sticker
    if ud.get("waiting") == "add":
        await add_to_set(update, context)
        return
    if not sticker.set_name:
        await say(update, context, T(context, "no_set_sticker"))
        return
    mode = ud.get("waiting")
    reset(context)
    ud["tg_set"] = sticker.set_name
    if mode == "download_link":
        await start_telegram_zip(update, context)
        return
    record = await owned_set(context, update.effective_user.id, name=sticker.set_name)
    if record is None:
        await say(
            update,
            context,
            T(context, "sticker_from", name=esc(sticker.set_name)),
            kb([Btn(T(context, "btn.download_whole_set"), callback_data="tg:zip")]),
        )
        return
    ud["sticker"] = {"file_id": sticker.file_id, "unique": sticker.file_unique_id, "set": record.name}
    ud["set_name"] = record.name
    await say(
        update,
        context,
        T(context, "sticker_yours", title=esc(record.title)),
        kb(
            [
                Btn(T(context, "btn.delete"), callback_data="stk:del"),
                Btn(T(context, "btn.move"), callback_data="stk:move"),
                Btn(T(context, "btn.emoji"), callback_data="stk:emoji"),
            ],
            [
                Btn(T(context, "btn.download_set"), callback_data="tg:zip"),
                Btn(T(context, "btn.set_menu"), callback_data=f"open:{record.id}"),
            ],
        ),
    )


async def current_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[dict, SetRecord] | None:
    info = context.user_data.get("sticker")
    if not info:
        return None
    record = await owned_set(context, update.effective_user.id, name=info["set"])
    return (info, record) if record else None


async def got_move(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    found = await current_sticker(update, context)
    if not found:
        reset(context)
        await say(update, context, T(context, "send_sticker_move_first"))
        return
    info, record = found
    sticker_set = await telegram_sets.get_set(context.bot, record.name)
    total = len(sticker_set.stickers) if sticker_set else 0
    try:
        position = int(text.strip().lstrip("#"))
    except ValueError:
        position = 0
    if not 1 <= position <= total:
        await say(update, context, T(context, "number_range", total=total))
        return
    await telegram_sets.call(context.bot.set_sticker_position_in_set, info["file_id"], position - 1)
    reset(context)
    await say(update, context, T(context, "moved", position=position))


async def got_sticker_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    found = await current_sticker(update, context)
    if not found:
        reset(context)
        await say(update, context, T(context, "send_sticker_change_first"))
        return
    emojis = parse_emojis(text)
    if not emojis:
        await say(update, context, T(context, "not_emoji"))
        return
    info, _ = found
    await telegram_sets.call(context.bot.set_sticker_emoji_list, info["file_id"], emojis)
    reset(context)
    await say(update, context, T(context, "emoji_updated", emojis="".join(emojis)))


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    await query.answer()
    ud = context.user_data
    user_id = update.effective_user.id

    # Main menu
    if data == "menu:import":
        await cmd_import(update, context)
    elif data == "menu:manage":
        await cmd_manage(update, context)
    elif data == "menu:download":
        await cmd_download(update, context)

    # Language
    elif data in ("lang:en", "lang:zh", "lang:auto"):
        await set_language(update, context, data.split(":", 1)[1])

    # A LINE link was found
    elif data == "line:make":
        await start_import(update, context)
    elif data == "line:zip":
        await start_line_zip(update, context)

    # Import questions
    elif data == "imp:title":
        if "import" in ud:
            await ask_import_emoji(update, context)
        else:
            await say(update, context, T(context, "lost"))
    elif data == "imp:emoji":
        await launch_import(update, context, [cfg(context).default_emoji])
    elif data == "imp:copy":
        await start_import(update, context, copy=True)
    elif data == "imp:cancel":
        await cmd_cancel(update, context)

    # Download a Telegram set
    elif data == "tg:zip":
        await start_telegram_zip(update, context)

    # Pick a set from "My sets"
    elif data.startswith("open:"):
        record = await owned_set(context, user_id, set_id=data.split(":", 1)[1])
        if record:
            reset(context)
            await show_set_menu(update, context, record)
        else:
            await say(update, context, T(context, "not_yours"))

    # Actions on the open set
    elif data.startswith("set:"):
        record = await current_set(update, context)
        if record is None:
            await say(update, context, T(context, "pick_set_first"))
            return
        action = data.split(":", 1)[1]
        if action == "add":
            reset(context)
            ud["waiting"] = "add"
            await say(update, context, T(context, "add_prompt", emoji=cfg(context).default_emoji), done_button(context))
        elif action == "rename":
            reset(context)
            ud["waiting"] = "rename"
            await say(update, context, T(context, "rename_prompt"))
        elif action == "zip":
            ud["tg_set"] = record.name
            await start_telegram_zip(update, context)
        elif action == "delete":
            await say(
                update,
                context,
                T(context, "confirm_delete_set", title=esc(record.title)),
                kb([
                    Btn(T(context, "btn.yes_delete"), callback_data="set:delete!"),
                    Btn(T(context, "btn.no"), callback_data=f"open:{record.id}"),
                ]),
            )
        elif action == "delete!":
            await telegram_sets.call(context.bot.delete_sticker_set, record.name)
            await store(context).delete(record.name)
            reset(context)
            ud.pop("set_name", None)
            await say(update, context, T(context, "set_deleted"))

    elif data == "add:done":
        await cmd_done(update, context)

    # Actions on one sticker
    elif data.startswith("stk:"):
        found = await current_sticker(update, context)
        if not found:
            await say(update, context, T(context, "send_sticker_again"))
            return
        info, record = found
        action = data.split(":", 1)[1]
        if action == "del":
            await say(
                update,
                context,
                T(context, "confirm_delete_sticker"),
                kb([
                    Btn(T(context, "btn.yes_delete"), callback_data="stk:del!"),
                    Btn(T(context, "btn.no"), callback_data="stk:keep"),
                ]),
            )
        elif action == "del!":
            await telegram_sets.call(context.bot.delete_sticker_from_set, info["file_id"])
            reset(context)
            await say(update, context, T(context, "sticker_deleted"))
        elif action == "keep":
            await say(update, context, T(context, "kept"))
        elif action == "move":
            sticker_set = await telegram_sets.get_set(context.bot, record.name)
            stickers = sticker_set.stickers if sticker_set else []
            now = next((i for i, s in enumerate(stickers, 1) if s.file_unique_id == info["unique"]), None)
            ud["waiting"] = "move"
            where = T(context, "move_where", now=now, total=len(stickers)) if now else ""
            await say(update, context, T(context, "move_prompt", where=where, total=len(stickers)))
        elif action == "emoji":
            ud["waiting"] = "sticker_emoji"
            await say(update, context, T(context, "emoji_prompt"))


# --------------------------------------------------------------------------
# Errors and start-up
# --------------------------------------------------------------------------


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    log.error("Error while handling an update", exc_info=error)
    if isinstance(update, Update) and update.effective_chat:
        lang = L(context) if context.user_data is not None else "en"
        if isinstance(error, BadRequest):
            text = i18n.t(lang, "err.telegram_refused", reason=esc(error.message))
        elif isinstance(error, UserError):
            text = f"😕 {esc(error.text(lang))}"
        else:
            text = i18n.t(lang, "err.generic")
        await safe_send(context, update.effective_chat.id, text)


COMMANDS = ("start", "import", "manage", "download", "language", "cancel")


async def post_init(app: Application) -> None:
    config: Config = app.bot_data["config"]
    app.bot_data["jobs"] = asyncio.Semaphore(config.max_jobs)
    app.bot_data.setdefault("active_jobs", 0)
    if not convert.ffmpeg_available():
        log.warning("ffmpeg/ffprobe not found - animated stickers will fail. Install ffmpeg.")
    # The "/" menu in Telegram: English by default, Chinese for people whose app is in Chinese.
    for lang, language_code in (("en", None), ("zh", "zh")):
        try:
            await app.bot.set_my_commands(
                [BotCommand(name, i18n.t(lang, f"cmd.{name}")) for name in COMMANDS],
                language_code=language_code,
            )
        except TelegramError as exc:
            log.warning("Couldn't set the command menu (%s): %s", lang, exc)
    log.info("Bot @%s is running.", app.bot.username)


def make_storage(config: Config) -> SqliteStorage | RemoteStorage:
    """On Cloudflare (STORE_URL set) the list of sets lives in D1; everywhere else in a local file."""
    if config.store_url:
        return RemoteStorage(config.store_url)
    return SqliteStorage(config.db_path)


def build_app(
    config: Config,
    storage: SqliteStorage | RemoteStorage | None = None,
    request: BaseRequest | None = None,
) -> Application:
    builder: ApplicationBuilder = Application.builder().token(config.bot_token).post_init(post_init)
    builder = builder.concurrent_updates(16)
    if config.telegram_api_url:  # only used by automated tests
        base = config.telegram_api_url.rstrip("/")
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
    if request is not None:  # used by the automated tests to fake Telegram
        builder = builder.request(request).get_updates_request(request)
    else:
        builder = builder.connect_timeout(15).read_timeout(30).write_timeout(60).media_write_timeout(120)
    app = builder.build()
    app.bot_data["config"] = config
    app.bot_data["storage"] = storage or make_storage(config)
    app.bot_data["active_jobs"] = 0

    app.add_handler(TypeHandler(Update, gatekeeper), group=-1)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("import", cmd_import))
    app.add_handler(CommandHandler("manage", cmd_manage))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler(["language", "lang"], cmd_language))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Sticker.ALL, on_sticker))
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.ANIMATION | filters.VIDEO | filters.Document.ALL, on_media)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app
