"""The chat part of the bot: what it says, which buttons it shows, and what
happens when you tap a button or send it something.

How a conversation works, in plain words:
  * The bot remembers, per person, what it's waiting for (a set name, an emoji,
    pictures to add...). That note is kept in `context.user_data["waiting"]`.
  * Every message you send is looked at in light of that note.
  * Buttons carry a short code (like "set:rename") that tells the bot which
    button was tapped.
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

from . import convert, jobs, line_store, telegram_sets
from .config import Config
from .storage import RemoteStorage, SetRecord, SqliteStorage, set_link

log = logging.getLogger(__name__)

MAX_BOT_DOWNLOAD = 20 * 1024 * 1024  # Telegram lets bots download files up to 20 MB
MAX_BOT_UPLOAD = 49 * 1024 * 1024  # ...and send files up to 50 MB

HELP = """👋 <b>Hi! I turn LINE stickers into Telegram stickers.</b>

<b>To convert a pack</b>, just send me its LINE link, for example
https://store.line.me/stickershop/product/11537/en
Still and animated stickers both work. LINE emoji packs become Telegram custom emoji.

<b>Other things I can do</b>
/manage – edit sets I made for you (add, delete, move, change emoji, rename)
/download – get a ZIP of a LINE pack or any Telegram sticker set
/cancel – stop whatever we're doing

💡 Send me any sticker to download its whole set, or to edit it if it's yours."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def kb(*rows: list[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])


MAIN_MENU = kb(
    [Btn("📥 Import LINE stickers", callback_data="menu:import")],
    [Btn("🛠 My sets", callback_data="menu:manage"), Btn("📦 Download ZIP", callback_data="menu:download")],
)


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


def background(context: ContextTypes.DEFAULT_TYPE, chat_id: int, work, start_text: str = "⏳ Getting started…"):
    """Run a slow job without freezing the bot. Only one job per person at a time;
    across everyone, at most MAX_JOBS run together (the rest wait their turn)."""
    user_data = context.user_data
    user_data["busy"] = True
    slots: asyncio.Semaphore = context.bot_data["jobs"]
    bot_data = context.bot_data
    # Counted so the Cloudflare host knows not to put the bot to sleep mid-job.
    bot_data["active_jobs"] = bot_data.get("active_jobs", 0) + 1

    async def runner():
        try:
            text = "🕒 Waiting for a free slot – other people's packs are being converted…" if slots.locked() else start_text
            message = await context.bot.send_message(chat_id, text)
            async with slots:
                await work(jobs.Progress(message))
        except (line_store.LineError, convert.ConvertError) as exc:
            await safe_send(context, chat_id, f"😕 {esc(exc)}")
        except BadRequest as exc:
            log.warning("Job failed: %s", exc)
            await safe_send(context, chat_id, f"😕 Telegram refused: {esc(exc.message)}")
        except Exception as exc:  # report anything unexpected instead of failing silently
            log.exception("Job failed")
            await safe_send(context, chat_id, f"😵 Sorry, that failed: {esc(str(exc))[:400]}")
        finally:
            user_data["busy"] = False
            bot_data["active_jobs"] = max(0, bot_data.get("active_jobs", 1) - 1)

    return context.application.create_task(runner())


async def ensure_not_busy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("busy"):
        await say(update, context, "⏳ I'm still working on your previous request – please wait for it to finish.")
        return False
    return True


# --------------------------------------------------------------------------
# Who may use the bot
# --------------------------------------------------------------------------


async def gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before everything else. Ignores group chats and, if ALLOWED_USER_IDS is set, strangers."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or chat.type != ChatType.PRIVATE:
        raise ApplicationHandlerStop
    if not cfg(context).is_allowed(user.id):
        if update.callback_query:
            await update.callback_query.answer("Sorry, this is a private bot.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(
                f"Sorry, this is a private bot. (Your Telegram user ID is {user.id}.)"
            )
        raise ApplicationHandlerStop


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    await say(update, context, HELP, MAIN_MENU)


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    context.user_data["waiting"] = "import_link"
    await say(
        update,
        context,
        "📥 Send me a LINE sticker or emoji link.\n\nIn the LINE app: open the sticker shop page → Share → Copy link. "
        "Or copy the address from store.line.me.",
    )


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    context.user_data["waiting"] = "download_link"
    await say(
        update,
        context,
        "📦 Send me one of these and I'll reply with a ZIP file:\n"
        "• a LINE sticker or emoji link\n• a Telegram set link (t.me/addstickers/…)\n• any sticker from the set",
    )


async def cmd_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    sets = await store(context).list_for_owner(update.effective_user.id)
    if not sets:
        await say(update, context, "You don't have any sets yet. Send me a LINE sticker link to make one! ✨")
        return
    rows = [
        [Btn(("😀 " if s.sticker_type == "custom_emoji" else "🖼 ") + s.title[:45], callback_data=f"open:{s.id}")]
        for s in sets[:60]
    ]
    await say(update, context, "🛠 <b>Your sets</b> – pick one:", kb(*rows))


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await say(update, context, f"Your Telegram user ID is <code>{update.effective_user.id}</code>")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    await say(update, context, "👌 Cancelled. Send me a LINE link whenever you're ready.")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    was_adding = context.user_data.get("waiting") == "add"
    reset(context)
    record = await owned_set(context, update.effective_user.id, name=context.user_data.get("set_name"))
    if was_adding and record:
        await show_set_menu(update, context, record)
    else:
        await say(update, context, "👌 All done.")


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
        await say(update, context, "Send me pictures, GIFs, videos or stickers to add – or tap Done.", kb(
            [Btn("✅ Done", callback_data="add:done")]))
        return
    await say(
        update,
        context,
        "🤔 I didn't catch that. Send me a LINE sticker link, for example\n"
        "https://store.line.me/stickershop/product/11537/en\n\nOr /help to see everything I can do.",
    )


# --------------------------------------------------------------------------
# LINE links: import or download
# --------------------------------------------------------------------------


async def got_line_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: line_store.LineLink) -> None:
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        pack = await line_store.fetch_info(link)
    except line_store.LineError as exc:
        await say(update, context, f"😕 {esc(exc)}")
        return
    ud = context.user_data
    mode = ud.get("waiting")
    reset(context)
    ud["line"] = {"link": link, "title": pack.title, "describe": pack.describe(), "kind": pack.kind}

    if mode == "import_link":
        await start_import(update, context)
        return
    if mode == "download_link":
        await start_line_zip(update, context)
        return
    note = (
        "\n\n😀 LINE emoji become Telegram <b>custom emoji</b>. Sending them in chats needs Telegram Premium."
        if pack.kind == "emoji"
        else ""
    )
    await say(
        update,
        context,
        f"🔎 Found <b>{esc(pack.title)}</b>\n{esc(pack.describe())}{note}\n\nWhat would you like to do?",
        kb(
            [Btn("📥 Make a Telegram set", callback_data="line:make")],
            [Btn("📦 Download originals (ZIP)", callback_data="line:zip")],
        ),
    )


async def start_import(update: Update, context: ContextTypes.DEFAULT_TYPE, copy: bool = False) -> None:
    ud = context.user_data
    line = ud.get("line")
    if not line:
        await say(update, context, "Please send me the LINE link first.")
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
            if record and record.owner_id == update.effective_user.id:
                text = f"✅ You already made this one: <a href=\"{url}\">{esc(existing.title)}</a>"
            else:
                text = (
                    f"✨ Someone already converted this pack: <a href=\"{url}\">{esc(existing.title)}</a>\n\n"
                    "You can add it right away, or make your own copy that you can edit."
                )
            await say(update, context, text, kb(
                [Btn("🔗 Open the set", url=url)],
                [Btn("🆕 Make my own copy", callback_data="imp:copy")],
            ))
            return

    ud["import"] = {"link": link, "set_name": name, "title": line["title"][: telegram_sets.NAME_MAX]}
    ud["waiting"] = "title"
    await say(
        update,
        context,
        f"📝 What should the Telegram set be called?\n\nTap the button to keep <b>{esc(ud['import']['title'])}</b>, "
        "or type a new name (up to 64 characters).",
        kb([Btn("✅ Keep this name", callback_data="imp:title")], [Btn("✖️ Cancel", callback_data="imp:cancel")]),
    )


async def got_import_title(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    title = " ".join(text.split())[: telegram_sets.NAME_MAX]
    if not title or "import" not in context.user_data:
        await say(update, context, "Please type a name for the set (or /cancel).")
        return
    context.user_data["import"]["title"] = title
    await ask_import_emoji(update, context)


async def ask_import_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["waiting"] = "emoji"
    default = cfg(context).default_emoji
    await say(
        update,
        context,
        "😀 Which emoji should the stickers be tagged with?\n\nTelegram needs at least one emoji per sticker – "
        "it's what people search by. Send one or more emoji (e.g. 😂❤️), or tap the button.",
        kb([Btn(f"Use {default} for all", callback_data="imp:emoji")], [Btn("✖️ Cancel", callback_data="imp:cancel")]),
    )


async def got_import_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    emojis = parse_emojis(text)
    if not emojis:
        await say(update, context, "That doesn't look like an emoji – please send something like 😀 (or /cancel).")
        return
    await launch_import(update, context, emojis)


async def launch_import(update: Update, context: ContextTypes.DEFAULT_TYPE, emojis: list[str]) -> None:
    ud = context.user_data
    plan = ud.get("import")
    if not plan:
        await say(update, context, "Something got lost – please send the LINE link again.")
        return
    if not await ensure_not_busy(update, context):
        return
    reset(context)
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    request = jobs.ImportRequest(
        user_id=user_id, link=plan["link"], title=plan["title"], set_name=plan["set_name"], emojis=emojis
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
        noun = "emoji" if request.sticker_type == "custom_emoji" else "stickers"
        text = f"🎉 <b>Done!</b> {esc(result.title)} – {result.added} {noun} added.\n{result.link}"
        if result.failed:
            shown = "\n".join(esc(f) for f in result.failed[:8])
            text += f"\n\n⚠️ {len(result.failed)} couldn't be added:\n{shown}"
        await progress.update("✅ Finished.", force=True)
        rows = [[Btn("➕ Add to Telegram", url=result.link)]]
        if record:
            rows.append([Btn("🛠 Manage this set", callback_data=f"open:{record.id}")])
        await safe_send(context, chat_id, text, kb(*rows))

    background(context, chat_id, work, "⬇️ Downloading from LINE…")


async def start_line_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    line = context.user_data.get("line")
    if not line:
        await say(update, context, "Please send me the LINE link first.")
        return
    if not await ensure_not_busy(update, context):
        return
    chat_id = update.effective_chat.id
    link = line["link"]

    async def work(progress: jobs.Progress) -> None:
        filename, data, caption = await jobs.run_line_zip(link, progress)
        await send_zip(context, chat_id, progress, filename, data, caption)

    background(context, chat_id, work, "⬇️ Downloading from LINE…")


async def send_zip(context, chat_id: int, progress: jobs.Progress, filename: str, data: bytes, caption: str) -> None:
    if len(data) > MAX_BOT_UPLOAD:
        raise RuntimeError("The ZIP is bigger than the 50 MB Telegram lets bots send.")
    await progress.update("📤 Sending the ZIP…", force=True)
    await telegram_sets.call(
        context.bot.send_document,
        chat_id,
        InputFile(data, filename=filename),
        caption=caption,
        parse_mode=ParseMode.HTML,
        attempts=3,
    )
    await progress.update("✅ Finished.", force=True)


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
        await say(update, context, "😕 I can't find that Telegram sticker set.")
        return
    rows = [[Btn("📦 Download as ZIP", callback_data="tg:zip")]]
    record = await owned_set(context, update.effective_user.id, name=name)
    if record:
        rows.append([Btn("🛠 Manage this set", callback_data=f"open:{record.id}")])
    await say(
        update,
        context,
        f"🖼 <b>{esc(sticker_set.title)}</b> – {len(sticker_set.stickers)} stickers",
        kb(*rows),
    )


async def start_telegram_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = context.user_data.get("tg_set")
    if not name:
        await say(update, context, "Send me the sticker set link (or a sticker from it) first.")
        return
    if not await ensure_not_busy(update, context):
        return
    chat_id = update.effective_chat.id
    bot = context.bot

    async def work(progress: jobs.Progress) -> None:
        filename, data, caption = await jobs.run_telegram_zip(bot, name, progress)
        await send_zip(context, chat_id, progress, filename, data, caption)

    background(context, chat_id, work, "⬇️ Fetching sticker set…")


# --------------------------------------------------------------------------
# Managing a set
# --------------------------------------------------------------------------


async def show_set_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, record: SetRecord) -> None:
    sticker_set = await telegram_sets.get_set(context.bot, record.name)
    if sticker_set is None:
        await store(context).delete(record.name)
        await say(
            update,
            context,
            "That set no longer exists on Telegram (maybe it was deleted in the app), so I've removed it from your list.",
        )
        return
    context.user_data.pop("waiting", None)
    context.user_data["set_name"] = record.name
    noun = "emoji" if record.sticker_type == "custom_emoji" else "stickers"
    limit = telegram_sets.set_limit(record.sticker_type)
    await say(
        update,
        context,
        f"🛠 <b>{esc(sticker_set.title)}</b>\n{record.link}\n{len(sticker_set.stickers)}/{limit} {noun}\n\n"
        "💡 To delete, move or change the emoji of a single sticker, just send me that sticker.",
        kb(
            [Btn("➕ Add stickers", callback_data="set:add"), Btn("✏️ Rename", callback_data="set:rename")],
            [Btn("📦 Download ZIP", callback_data="set:zip"), Btn("🗑 Delete set", callback_data="set:delete")],
            [Btn("« My sets", callback_data="menu:manage")],
        ),
    )


async def current_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> SetRecord | None:
    return await owned_set(context, update.effective_user.id, name=context.user_data.get("set_name"))


async def got_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    record = await current_set(update, context)
    title = " ".join(text.split())[: telegram_sets.NAME_MAX]
    if not record or not title:
        reset(context)
        await say(update, context, "Pick a set first: /manage")
        return
    await telegram_sets.call(context.bot.set_sticker_set_title, record.name, title)
    await store(context).rename(record.name, title)
    reset(context)
    await say(update, context, f"✏️ Renamed to <b>{esc(title)}</b>.")
    await show_set_menu(update, context, await store(context).get_by_name(record.name))


async def add_to_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add whatever the person just sent (picture, GIF, video, sticker) to the set they're editing."""
    msg = update.effective_message
    record = await current_set(update, context)
    if record is None:
        reset(context)
        await say(update, context, "Pick a set first: /manage")
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
                await msg.reply_text("Sorry, animated .tgs stickers can't be shrunk into emoji.")
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
            await msg.reply_text("Please send a picture, GIF, short video or sticker.")
            return
        if (file_obj.file_size or 0) > MAX_BOT_DOWNLOAD:
            await msg.reply_text("That file is over 20 MB, which is too big for bots to download.")
            return

        sticker_set = await telegram_sets.get_set(context.bot, record.name)
        if sticker_set is None:
            await msg.reply_text("That set doesn't exist on Telegram any more.")
            return
        count = len(sticker_set.stickers)
        if count >= telegram_sets.set_limit(record.sticker_type):
            await msg.reply_text("This set is full – Telegram doesn't allow any more in it.")
            return

        if sticker_input is None:
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.CHOOSE_STICKER)
            data = await download_telegram_file(context, file_obj.file_id)
            try:
                converted = await convert.convert(data, emoji=emoji_mode)
            except convert.ConvertError as exc:
                await msg.reply_text(f"😕 {exc}")
                return
            sticker_input = InputSticker(
                InputFile(converted.data, filename=converted.filename, attach=True),  # see jobs.py
                emojis or [cfg(context).default_emoji],
                converted.format,
            )
        try:
            await telegram_sets.add_sticker(context.bot, record.owner_id, record.name, sticker_input, count)
        except BadRequest as exc:
            await msg.reply_text(f"😕 Telegram refused it: {exc.message}")
            return
        await msg.reply_text(
            f"✅ Added! The set now has {count + 1}. Send more, or tap Done.\n"
            "💡 Put an emoji in the caption to choose this sticker's emoji.",
            reply_markup=kb([Btn("✅ Done", callback_data="add:done")]),
        )


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("waiting") == "add":
        await add_to_set(update, context)
        return
    await say(
        update,
        context,
        "To add this to one of your sets: /manage → pick the set → ➕ Add stickers.",
    )


async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud = context.user_data
    sticker = update.effective_message.sticker
    if ud.get("waiting") == "add":
        await add_to_set(update, context)
        return
    if not sticker.set_name:
        await say(update, context, "That sticker isn't part of a set, so there's nothing to download or edit.")
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
            f"🖼 This sticker is from the set <code>{esc(sticker.set_name)}</code>.",
            kb([Btn("📦 Download the whole set (ZIP)", callback_data="tg:zip")]),
        )
        return
    ud["sticker"] = {"file_id": sticker.file_id, "unique": sticker.file_unique_id, "set": record.name}
    ud["set_name"] = record.name
    await say(
        update,
        context,
        f"🖼 This sticker is in your set <b>{esc(record.title)}</b>. What would you like to do?",
        kb(
            [
                Btn("🗑 Delete", callback_data="stk:del"),
                Btn("↕️ Move", callback_data="stk:move"),
                Btn("😀 Emoji", callback_data="stk:emoji"),
            ],
            [Btn("📦 Download set", callback_data="tg:zip"), Btn("🛠 Set menu", callback_data=f"open:{record.id}")],
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
        await say(update, context, "Send me the sticker you want to move first.")
        return
    info, record = found
    sticker_set = await telegram_sets.get_set(context.bot, record.name)
    total = len(sticker_set.stickers) if sticker_set else 0
    try:
        position = int(text.strip().lstrip("#"))
    except ValueError:
        position = 0
    if not 1 <= position <= total:
        await say(update, context, f"Please send a number from 1 to {total} (or /cancel).")
        return
    await telegram_sets.call(context.bot.set_sticker_position_in_set, info["file_id"], position - 1)
    reset(context)
    await say(update, context, f"↕️ Moved to position {position}.")


async def got_sticker_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    found = await current_sticker(update, context)
    if not found:
        reset(context)
        await say(update, context, "Send me the sticker you want to change first.")
        return
    emojis = parse_emojis(text)
    if not emojis:
        await say(update, context, "That doesn't look like an emoji – please send something like 😀 (or /cancel).")
        return
    info, _ = found
    await telegram_sets.call(context.bot.set_sticker_emoji_list, info["file_id"], emojis)
    reset(context)
    await say(update, context, f"😀 Updated – this sticker now has {''.join(emojis)}")


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
            await say(update, context, "Something got lost – please send the LINE link again.")
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
            await say(update, context, "That set isn't yours (or no longer exists).")

    # Actions on the open set
    elif data.startswith("set:"):
        record = await current_set(update, context)
        if record is None:
            await say(update, context, "Pick a set first: /manage")
            return
        action = data.split(":", 1)[1]
        if action == "add":
            reset(context)
            ud["waiting"] = "add"
            await say(
                update,
                context,
                "➕ Send me pictures (PNG, JPG, WEBP), GIFs, short videos or stickers – as many as you like, "
                "and I'll add each one.\n\n💡 Put an emoji in the caption to choose its emoji; "
                f"otherwise I'll use {cfg(context).default_emoji}.",
                kb([Btn("✅ Done", callback_data="add:done")]),
            )
        elif action == "rename":
            reset(context)
            ud["waiting"] = "rename"
            await say(update, context, "✏️ Send me the new name for the set (up to 64 characters).")
        elif action == "zip":
            ud["tg_set"] = record.name
            await start_telegram_zip(update, context)
        elif action == "delete":
            await say(
                update,
                context,
                f"🗑 Delete <b>{esc(record.title)}</b> for everyone? This can't be undone.",
                kb([Btn("Yes, delete it", callback_data="set:delete!"), Btn("No", callback_data=f"open:{record.id}")]),
            )
        elif action == "delete!":
            await telegram_sets.call(context.bot.delete_sticker_set, record.name)
            await store(context).delete(record.name)
            reset(context)
            ud.pop("set_name", None)
            await say(update, context, "🗑 The set has been deleted.")

    elif data == "add:done":
        await cmd_done(update, context)

    # Actions on one sticker
    elif data.startswith("stk:"):
        found = await current_sticker(update, context)
        if not found:
            await say(update, context, "Send me the sticker again, please.")
            return
        info, record = found
        action = data.split(":", 1)[1]
        if action == "del":
            await say(
                update,
                context,
                "🗑 Delete this sticker from the set?",
                kb([Btn("Yes, delete it", callback_data="stk:del!"), Btn("No", callback_data="stk:keep")]),
            )
        elif action == "del!":
            await telegram_sets.call(context.bot.delete_sticker_from_set, info["file_id"])
            reset(context)
            await say(update, context, "🗑 Deleted. (It may take a minute to disappear in your app.)")
        elif action == "keep":
            await say(update, context, "👌 Kept it.")
        elif action == "move":
            sticker_set = await telegram_sets.get_set(context.bot, record.name)
            stickers = sticker_set.stickers if sticker_set else []
            now = next((i for i, s in enumerate(stickers, 1) if s.file_unique_id == info["unique"]), None)
            ud["waiting"] = "move"
            where = f"It's currently number {now} of {len(stickers)}. " if now else ""
            await say(update, context, f"↕️ {where}Send me the new position (1–{len(stickers)}).")
        elif action == "emoji":
            ud["waiting"] = "sticker_emoji"
            await say(update, context, "😀 Send me the new emoji for this sticker (one or more).")


# --------------------------------------------------------------------------
# Errors and start-up
# --------------------------------------------------------------------------


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    log.error("Error while handling an update", exc_info=error)
    if isinstance(update, Update) and update.effective_chat:
        if isinstance(error, BadRequest):
            text = f"😕 Telegram refused: {esc(error.message)}"
        elif isinstance(error, line_store.LineError):
            text = f"😕 {esc(error)}"
        else:
            text = "😵 Sorry, something went wrong. Please try again."
        await safe_send(context, update.effective_chat.id, text)


async def post_init(app: Application) -> None:
    config: Config = app.bot_data["config"]
    app.bot_data["jobs"] = asyncio.Semaphore(config.max_jobs)
    app.bot_data.setdefault("active_jobs", 0)
    if not convert.ffmpeg_available():
        log.warning("ffmpeg/ffprobe not found - animated stickers will fail. Install ffmpeg.")
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Welcome & help"),
                BotCommand("import", "Convert a LINE sticker or emoji pack"),
                BotCommand("manage", "Edit the sets I made for you"),
                BotCommand("download", "Get a ZIP of a LINE pack or Telegram set"),
                BotCommand("cancel", "Stop what we're doing"),
            ]
        )
    except TelegramError as exc:
        log.warning("Couldn't set the command menu: %s", exc)
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
