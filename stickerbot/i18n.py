"""Everything the bot says, in English and Traditional Chinese (Hong Kong).

How it works, in plain words:
  * Every message has a short name (a "key"), e.g. "ask_title".
  * TEXTS below holds each message in both languages.
  * The bot calls t(lang, "ask_title", title="Brown & Cony") and gets the right
    wording for that person. Words in {curly brackets} are filled in.

To change any wording, edit the text here - nothing else needs to change.

Which language does a person get?
  * If they chose one with /language, that one (it's remembered in the database).
  * Otherwise their Telegram app's language: Chinese (any kind) -> Traditional
    Chinese, anything else -> English.
"""

from __future__ import annotations

LANGS = ("en", "zh")


def lang_from_telegram(code: str | None) -> str:
    """Telegram tells bots each person's app language, e.g. "zh-hant", "zh-hk", "en"."""
    return "zh" if code and code.lower().startswith("zh") else "en"


class UserError(Exception):
    """A problem to explain to the person, in their language.

    raise UserError("set_full") ... the bot shows t(lang, "set_full").
    """

    def __init__(self, key: str, **params):
        self.key = key
        self.params = params
        super().__init__(t("en", key, **params))  # English version for the logs

    def text(self, lang: str) -> str:
        return t(lang, self.key, **self.params)


def t(lang: str, key: str, **params) -> str:
    entry = TEXTS[key]
    text = entry.get(lang) or entry["en"]
    return text.format(**params) if params else text


TEXTS: dict[str, dict[str, str]] = {
    # ---- Welcome / help --------------------------------------------------------
    "help": {
        "en": (
            "👋 <b>Hi! I turn LINE stickers into Telegram stickers.</b>\n\n"
            "<b>To convert a pack</b>, just send me its LINE link, for example\n"
            "https://store.line.me/stickershop/product/11537/en\n"
            "Still and animated stickers both work. LINE emoji packs become Telegram custom emoji.\n\n"
            "<b>Other things I can do</b>\n"
            "/manage – edit sets I made for you (add, delete, move, change emoji, rename)\n"
            "/download – get a ZIP of a LINE pack or any Telegram sticker set\n"
            "/language – 中文 / English\n"
            "/cancel – stop whatever we're doing\n\n"
            "💡 Send me any sticker to download its whole set, or to edit it if it's yours."
        ),
        "zh": (
            "👋 <b>你好！我可以把 LINE 貼圖變成 Telegram 貼圖。</b>\n\n"
            "<b>轉換貼圖</b>：直接把 LINE 貼圖的連結傳給我，例如\n"
            "https://store.line.me/stickershop/product/11537/zh-Hant\n"
            "靜態和動態貼圖都支援。LINE 表情貼會變成 Telegram 自訂表情符號。\n\n"
            "<b>其他功能</b>\n"
            "/manage – 管理我為你製作的貼圖包（新增、刪除、移動、更改表情符號、改名）\n"
            "/download – 下載 LINE 貼圖或任何 Telegram 貼圖包的 ZIP 壓縮檔\n"
            "/language – 中文 / English\n"
            "/cancel – 取消目前的操作\n\n"
            "💡 把任何一張貼圖傳給我，就可以下載整個貼圖包；如果是你的貼圖包，還可以編輯。"
        ),
    },
    "btn.import": {"en": "📥 Import LINE stickers", "zh": "📥 匯入 LINE 貼圖"},
    "btn.my_sets": {"en": "🛠 My sets", "zh": "🛠 我的貼圖包"},
    "btn.download_zip": {"en": "📦 Download ZIP", "zh": "📦 下載 ZIP"},
    "didnt_understand": {
        "en": (
            "🤔 I didn't catch that. Send me a LINE sticker link, for example\n"
            "https://store.line.me/stickershop/product/11537/en\n\nOr /help to see everything I can do."
        ),
        "zh": (
            "🤔 我不太明白。請把 LINE 貼圖連結傳給我，例如\n"
            "https://store.line.me/stickershop/product/11537/zh-Hant\n\n或輸入 /help 查看所有功能。"
        ),
    },
    # ---- Language ---------------------------------------------------------------
    "lang_prompt": {"en": "🌐 Choose a language / 請選擇語言：", "zh": "🌐 請選擇語言 / Choose a language:"},
    "btn.lang_en": {"en": "English", "zh": "English"},
    "btn.lang_zh": {"en": "繁體中文（香港）", "zh": "繁體中文（香港）"},
    "btn.lang_auto": {"en": "🔄 Follow my Telegram app", "zh": "🔄 跟隨 Telegram app 語言"},
    "lang_set": {"en": "✅ I'll speak English from now on.", "zh": "✅ 之後我會用繁體中文。"},
    "lang_auto": {
        "en": "✅ I'll follow your Telegram app's language.",
        "zh": "✅ 我會跟隨你的 Telegram app 語言。",
    },
    # ---- General ----------------------------------------------------------------
    "getting_started": {"en": "⏳ Getting started…", "zh": "⏳ 準備中…"},
    "busy_wait": {
        "en": "🕒 Waiting for a free slot – other people's packs are being converted…",
        "zh": "🕒 排隊中 – 正在為其他人轉換貼圖，請稍候…",
    },
    "still_busy": {
        "en": "⏳ I'm still working on your previous request – please wait for it to finish.",
        "zh": "⏳ 我仍在處理你上一個要求，請等它完成。",
    },
    "err.telegram_refused": {"en": "😕 Telegram refused: {reason}", "zh": "😕 Telegram 拒絕了：{reason}"},
    "err.failed": {"en": "😵 Sorry, that failed: {reason}", "zh": "😵 抱歉，處理失敗：{reason}"},
    "err.generic": {
        "en": "😵 Sorry, something went wrong. Please try again.",
        "zh": "😵 抱歉，出了點問題，請再試一次。",
    },
    "private_bot_alert": {"en": "Sorry, this is a private bot.", "zh": "抱歉，這是私人機械人。"},
    "private_bot": {
        "en": "Sorry, this is a private bot. (Your Telegram user ID is {user_id}.)",
        "zh": "抱歉，這是私人機械人。（你的 Telegram 用戶 ID 是 {user_id}。）",
    },
    "myid": {
        "en": "Your Telegram user ID is <code>{user_id}</code>",
        "zh": "你的 Telegram 用戶 ID 是 <code>{user_id}</code>",
    },
    "cancelled": {
        "en": "👌 Cancelled. Send me a LINE link whenever you're ready.",
        "zh": "👌 已取消。準備好時再把 LINE 連結傳給我吧。",
    },
    "all_done": {"en": "👌 All done.", "zh": "👌 完成。"},
    "finished": {"en": "✅ Finished.", "zh": "✅ 完成。"},
    "lost": {
        "en": "Something got lost – please send the LINE link again.",
        "zh": "資料遺失了，請再傳一次 LINE 連結。",
    },
    "btn.done": {"en": "✅ Done", "zh": "✅ 完成"},
    "btn.cancel": {"en": "✖️ Cancel", "zh": "✖️ 取消"},
    "btn.yes_delete": {"en": "Yes, delete it", "zh": "是，刪除"},
    "btn.no": {"en": "No", "zh": "不要"},
    "noun.stickers": {"en": "stickers", "zh": "張貼圖"},
    "noun.emoji": {"en": "emoji", "zh": "個表情符號"},
    # ---- Import ------------------------------------------------------------------
    "import_prompt": {
        "en": (
            "📥 Send me a LINE sticker or emoji link.\n\nIn the LINE app: open the sticker shop page → Share → "
            "Copy link. Or copy the address from store.line.me."
        ),
        "zh": (
            "📥 請把 LINE 貼圖或表情貼的連結傳給我。\n\n在 LINE app 內：打開貼圖小舖的頁面 → 分享 → 複製連結。"
            "或者從 store.line.me 複製網址。"
        ),
    },
    "found_pack": {
        "en": "🔎 Found <b>{title}</b>\n{describe}{note}\n\nWhat would you like to do?",
        "zh": "🔎 找到 <b>{title}</b>\n{describe}{note}\n\n你想怎樣做？",
    },
    "emoji_note": {
        "en": "\n\n😀 LINE emoji become Telegram <b>custom emoji</b>. Sending them in chats needs Telegram Premium.",
        "zh": "\n\n😀 LINE 表情貼會變成 Telegram <b>自訂表情符號</b>。要在對話中使用，需要 Telegram Premium。",
    },
    "btn.make_set": {"en": "📥 Make a Telegram set", "zh": "📥 製作 Telegram 貼圖包"},
    "btn.download_originals": {"en": "📦 Download originals (ZIP)", "zh": "📦 下載原始檔案（ZIP）"},
    "send_link_first": {"en": "Please send me the LINE link first.", "zh": "請先把 LINE 連結傳給我。"},
    "already_yours": {
        "en": '✅ You already made this one: <a href="{url}">{title}</a>',
        "zh": '✅ 你之前已經製作過這個：<a href="{url}">{title}</a>',
    },
    "already_converted": {
        "en": (
            '✨ Someone already converted this pack: <a href="{url}">{title}</a>\n\n'
            "You can add it right away, or make your own copy that you can edit."
        ),
        "zh": (
            '✨ 已經有人轉換過這個貼圖包：<a href="{url}">{title}</a>\n\n'
            "你可以直接加入，或者製作一份可以自己編輯的副本。"
        ),
    },
    "btn.open_set": {"en": "🔗 Open the set", "zh": "🔗 打開貼圖包"},
    "btn.own_copy": {"en": "🆕 Make my own copy", "zh": "🆕 製作我自己的副本"},
    "ask_title": {
        "en": (
            "📝 What should the Telegram set be called?\n\nTap the button to keep <b>{title}</b>, "
            "or type a new name (up to 64 characters)."
        ),
        "zh": "📝 Telegram 貼圖包要叫甚麼名字？\n\n按下按鈕保留 <b>{title}</b>，或者輸入新名稱（最多 64 個字元）。",
    },
    "btn.keep_name": {"en": "✅ Keep this name", "zh": "✅ 保留這個名稱"},
    "type_name": {"en": "Please type a name for the set (or /cancel).", "zh": "請輸入貼圖包的名稱（或 /cancel 取消）。"},
    "ask_emoji": {
        "en": (
            "😀 Which emoji should the stickers be tagged with?\n\nTelegram needs at least one emoji per sticker – "
            "it's what people search by. Send one or more emoji (e.g. 😂❤️), or tap the button."
        ),
        "zh": (
            "😀 這些貼圖要配對哪個表情符號？\n\nTelegram 規定每張貼圖最少要有一個表情符號，用來搜尋貼圖。"
            "請傳送一個或多個表情符號（例如 😂❤️），或者按下按鈕。"
        ),
    },
    "btn.use_default_emoji": {"en": "Use {emoji} for all", "zh": "全部使用 {emoji}"},
    "not_emoji": {
        "en": "That doesn't look like an emoji – please send something like 😀 (or /cancel).",
        "zh": "這好像不是表情符號，請傳送例如 😀（或 /cancel 取消）。",
    },
    "downloading_line": {"en": "⬇️ Downloading from LINE…", "zh": "⬇️ 正在從 LINE 下載…"},
    "import_done": {
        "en": "🎉 <b>Done!</b> {title} – {count} {noun} added.\n{link}",
        "zh": "🎉 <b>完成！</b>{title} – 已加入 {count} {noun}。\n{link}",
    },
    "import_some_failed": {
        "en": "\n\n⚠️ {count} couldn't be added:\n{details}",
        "zh": "\n\n⚠️ 有 {count} 個無法加入：\n{details}",
    },
    "btn.add_to_telegram": {"en": "➕ Add to Telegram", "zh": "➕ 加入 Telegram"},
    "btn.manage_this_set": {"en": "🛠 Manage this set", "zh": "🛠 管理這個貼圖包"},
    "btn.retry_failed": {"en": "🔁 Retry the {count} that failed", "zh": "🔁 重試失敗的 {count} 個"},
    "retrying": {"en": "🔁 Retrying {count}…", "zh": "🔁 正在重試 {count} 個…"},
    "progress.retry": {"en": "🔁 Retrying {bar}", "zh": "🔁 重試中 {bar}"},
    "retry_done": {
        "en": "✅ Added {added} of the {count} that failed, each in its original place.\n{link}",
        "zh": "✅ 之前失敗的 {count} 個中，已加入 {added} 個，並放回原本的位置。\n{link}",
    },
    "retry_pack_changed": {
        "en": "The LINE pack has changed since the import, so I can't tell which stickers are missing. "
        "Delete the set and import it again.",
        "zh": "這個 LINE 貼圖包在匯入後有所改變，我無法判斷缺少哪些貼圖。請刪除貼圖包後重新匯入。",
    },
    "retry_nothing_missing": {
        "en": "Those stickers are already back in the set – nothing to retry.",
        "zh": "那些貼圖已經在貼圖包內，不需要重試。",
    },
    "retry_unavailable": {
        "en": "I don't know which LINE pack this set came from, so I can't retry it.",
        "zh": "我不知道這個貼圖包來自哪個 LINE 貼圖，所以無法重試。",
    },
    "progress.import": {"en": "⚙️ Converting {converted}\n📤 Uploading {uploaded}", "zh": "⚙️ 轉換中 {converted}\n📤 上載中 {uploaded}"},
    "telegram_said": {"en": "Telegram said “{reason}”", "zh": "Telegram 回覆「{reason}」"},
    "none_converted": {
        "en": "None of the stickers could be converted:\n{details}",
        "zh": "所有貼圖都無法轉換：\n{details}",
    },
    # ---- ZIP downloads -------------------------------------------------------------
    "download_prompt": {
        "en": (
            "📦 Send me one of these and I'll reply with a ZIP file:\n• a LINE sticker or emoji link\n"
            "• a Telegram set link (t.me/addstickers/…)\n• any sticker from the set"
        ),
        "zh": (
            "📦 把以下任何一樣傳給我，我會回覆一個 ZIP 壓縮檔：\n• LINE 貼圖或表情貼的連結\n"
            "• Telegram 貼圖包連結（t.me/addstickers/…）\n• 貼圖包內的任何一張貼圖"
        ),
    },
    "zip_too_big": {
        "en": "The ZIP is bigger than the 50 MB Telegram lets bots send.",
        "zh": "ZIP 檔超過 Telegram 容許機械人傳送的 50 MB 上限。",
    },
    "sending_zip": {"en": "📤 Sending the ZIP…", "zh": "📤 正在傳送 ZIP 檔…"},
    "fetching_set": {"en": "⬇️ Fetching sticker set…", "zh": "⬇️ 正在讀取貼圖包…"},
    "progress.download": {"en": "⬇️ Downloading {bar}", "zh": "⬇️ 下載中 {bar}"},
    "line_zip_caption": {"en": "📦 <b>{title}</b> — {describe}. {note}", "zh": "📦 <b>{title}</b> — {describe}。{note}"},
    "line_zip_animated_note": {
        "en": "Animated ones are animated PNG files – they play in web browsers.",
        "zh": "動態貼圖是 APNG 動態圖檔，可以用網頁瀏覽器播放。",
    },
    "tg_zip_caption": {"en": "📦 <b>{title}</b> — {count} stickers", "zh": "📦 <b>{title}</b> — {count} 張貼圖"},
    "set_not_found": {"en": "I can't find that Telegram sticker set.", "zh": "找不到這個 Telegram 貼圖包。"},
    "btn.download_as_zip": {"en": "📦 Download as ZIP", "zh": "📦 下載 ZIP"},
    "tg_set_info": {"en": "🖼 <b>{title}</b> – {count} stickers", "zh": "🖼 <b>{title}</b> – {count} 張貼圖"},
    "send_set_first": {
        "en": "Send me the sticker set link (or a sticker from it) first.",
        "zh": "請先把貼圖包連結（或其中一張貼圖）傳給我。",
    },
    # ---- Managing sets -------------------------------------------------------------
    "no_sets": {
        "en": "You don't have any sets yet. Send me a LINE sticker link to make one! ✨",
        "zh": "你還沒有任何貼圖包。把 LINE 貼圖連結傳給我就可以製作！✨",
    },
    "your_sets": {"en": "🛠 <b>Your sets</b> – pick one:", "zh": "🛠 <b>你的貼圖包</b> – 請選擇："},
    "set_gone": {
        "en": "That set no longer exists on Telegram (maybe it was deleted in the app), so I've removed it from your list.",
        "zh": "這個貼圖包已經不在 Telegram 上（可能已在 app 內刪除），所以我已把它從你的列表移除。",
    },
    "set_menu": {
        "en": (
            "🛠 <b>{title}</b>\n{link}\n{count}/{limit} {noun}\n\n"
            "💡 To delete, move or change the emoji of a single sticker, just send me that sticker."
        ),
        "zh": (
            "🛠 <b>{title}</b>\n{link}\n{count}/{limit} {noun}\n\n"
            "💡 想刪除、移動或更改單張貼圖的表情符號，直接把那張貼圖傳給我就可以。"
        ),
    },
    "btn.add_stickers": {"en": "➕ Add stickers", "zh": "➕ 新增貼圖"},
    "btn.rename": {"en": "✏️ Rename", "zh": "✏️ 改名"},
    "btn.delete_set": {"en": "🗑 Delete set", "zh": "🗑 刪除貼圖包"},
    "btn.back_sets": {"en": "« My sets", "zh": "« 我的貼圖包"},
    "pick_set_first": {"en": "Pick a set first: /manage", "zh": "請先選擇貼圖包：/manage"},
    "not_yours": {"en": "That set isn't yours (or no longer exists).", "zh": "這個貼圖包不是你的（或已不存在）。"},
    "rename_prompt": {
        "en": "✏️ Send me the new name for the set (up to 64 characters).",
        "zh": "✏️ 請傳送貼圖包的新名稱（最多 64 個字元）。",
    },
    "renamed": {"en": "✏️ Renamed to <b>{title}</b>.", "zh": "✏️ 已改名為 <b>{title}</b>。"},
    "confirm_delete_set": {
        "en": "🗑 Delete <b>{title}</b> for everyone? This can't be undone.",
        "zh": "🗑 要為所有人刪除 <b>{title}</b> 嗎？刪除後無法復原。",
    },
    "set_deleted": {"en": "🗑 The set has been deleted.", "zh": "🗑 貼圖包已刪除。"},
    "add_prompt": {
        "en": (
            "➕ Send me pictures (PNG, JPG, WEBP), GIFs, short videos or stickers – as many as you like, "
            "and I'll add each one.\n\n💡 Put an emoji in the caption to choose its emoji; otherwise I'll use {emoji}."
        ),
        "zh": (
            "➕ 把圖片（PNG、JPG、WEBP）、GIF、短片或貼圖傳給我，數量不限，我會逐一加入。\n\n"
            "💡 在說明文字加上表情符號就可以選擇配對；否則我會使用 {emoji}。"
        ),
    },
    "add_hint": {
        "en": "Send me pictures, GIFs, videos or stickers to add – or tap Done.",
        "zh": "請傳送要加入的圖片、GIF、影片或貼圖，或者按「完成」。",
    },
    "added": {
        "en": (
            "✅ Added! The set now has {count}. Send more, or tap Done.\n"
            "💡 Put an emoji in the caption to choose this sticker's emoji."
        ),
        "zh": "✅ 已加入！貼圖包現在有 {count} 張。可以繼續傳送，或者按「完成」。\n💡 在說明文字加上表情符號，就可以選擇這張貼圖的表情符號。",
    },
    "tgs_no_emoji": {
        "en": "Sorry, animated .tgs stickers can't be shrunk into emoji.",
        "zh": "抱歉，.tgs 動態貼圖無法縮小成表情符號。",
    },
    "send_media": {"en": "Please send a picture, GIF, short video or sticker.", "zh": "請傳送圖片、GIF、短片或貼圖。"},
    "file_too_big": {
        "en": "That file is over 20 MB, which is too big for bots to download.",
        "zh": "這個檔案超過 20 MB，機械人無法下載。",
    },
    "set_missing": {"en": "That set doesn't exist on Telegram any more.", "zh": "這個貼圖包已經不在 Telegram 上。"},
    "set_full": {
        "en": "This set is full – Telegram doesn't allow any more in it.",
        "zh": "這個貼圖包已滿，Telegram 不容許再加入。",
    },
    "media_how": {
        "en": "To add this to one of your sets: /manage → pick the set → ➕ Add stickers.",
        "zh": "要把它加入你的貼圖包：/manage → 選擇貼圖包 → ➕ 新增貼圖。",
    },
    # ---- Single stickers -------------------------------------------------------------
    "no_set_sticker": {
        "en": "That sticker isn't part of a set, so there's nothing to download or edit.",
        "zh": "這張貼圖不屬於任何貼圖包，所以無法下載或編輯。",
    },
    "sticker_from": {
        "en": "🖼 This sticker is from the set <code>{name}</code>.",
        "zh": "🖼 這張貼圖來自貼圖包 <code>{name}</code>。",
    },
    "btn.download_whole_set": {"en": "📦 Download the whole set (ZIP)", "zh": "📦 下載整個貼圖包（ZIP）"},
    "sticker_yours": {
        "en": "🖼 This sticker is in your set <b>{title}</b>. What would you like to do?",
        "zh": "🖼 這張貼圖在你的貼圖包 <b>{title}</b> 內。你想怎樣做？",
    },
    "btn.delete": {"en": "🗑 Delete", "zh": "🗑 刪除"},
    "btn.move": {"en": "↕️ Move", "zh": "↕️ 移動"},
    "btn.emoji": {"en": "😀 Emoji", "zh": "😀 表情符號"},
    "btn.download_set": {"en": "📦 Download set", "zh": "📦 下載貼圖包"},
    "btn.set_menu": {"en": "🛠 Set menu", "zh": "🛠 貼圖包選單"},
    "send_sticker_again": {"en": "Send me the sticker again, please.", "zh": "請再傳一次那張貼圖。"},
    "confirm_delete_sticker": {"en": "🗑 Delete this sticker from the set?", "zh": "🗑 要從貼圖包刪除這張貼圖嗎？"},
    "sticker_deleted": {
        "en": "🗑 Deleted. (It may take a minute to disappear in your app.)",
        "zh": "🗑 已刪除。（可能需要約一分鐘才會在 app 內消失。）",
    },
    "kept": {"en": "👌 Kept it.", "zh": "👌 已保留。"},
    "move_where": {"en": "It's currently number {now} of {total}. ", "zh": "它現在是第 {now} 張（共 {total} 張）。"},
    "move_prompt": {"en": "↕️ {where}Send me the new position (1–{total}).", "zh": "↕️ {where}請傳送新的位置（1–{total}）。"},
    "send_sticker_move_first": {"en": "Send me the sticker you want to move first.", "zh": "請先傳送你想移動的貼圖。"},
    "number_range": {
        "en": "Please send a number from 1 to {total} (or /cancel).",
        "zh": "請傳送 1 至 {total} 之間的數字（或 /cancel 取消）。",
    },
    "moved": {"en": "↕️ Moved to position {position}.", "zh": "↕️ 已移到第 {position} 位。"},
    "emoji_prompt": {
        "en": "😀 Send me the new emoji for this sticker (one or more).",
        "zh": "😀 請傳送這張貼圖的新表情符號（一個或多個）。",
    },
    "send_sticker_change_first": {
        "en": "Send me the sticker you want to change first.",
        "zh": "請先傳送你想更改的貼圖。",
    },
    "emoji_updated": {"en": "😀 Updated – this sticker now has {emojis}", "zh": "😀 已更新 – 這張貼圖現在配對 {emojis}"},
    # ---- Describing a LINE pack ---------------------------------------------------------
    "pack.describe": {"en": "{count} {style} {what}{extra}", "zh": "{count} 個{style}{what}{extra}"},
    "pack.animated": {"en": "animated", "zh": "動態"},
    "pack.still": {"en": "still", "zh": "靜態"},
    "pack.stickers": {"en": "stickers", "zh": "貼圖"},
    "pack.emoji": {"en": "emoji", "zh": "表情貼"},
    "pack.popup": {"en": " (pop-up effects)", "zh": "（全螢幕特效）"},
    "pack.sound": {
        "en": " (sound is dropped – Telegram stickers are silent)",
        "zh": "（聲音會被移除，Telegram 貼圖沒有聲音）",
    },
    "pack.custom_text": {
        "en": " (custom-text pack – the text you type in LINE won't be included)",
        "zh": "（自訂文字貼圖：你在 LINE 輸入的文字不會包括在內）",
    },
    # ---- Problems with LINE ---------------------------------------------------------------
    "line.too_big": {
        "en": "That LINE pack is unexpectedly large, so I stopped downloading it.",
        "zh": "這個 LINE 貼圖包異常地大，所以我停止了下載。",
    },
    "line.unreachable": {
        "en": "Couldn't reach LINE's servers ({error}). Please try again later.",
        "zh": "無法連接 LINE 的伺服器（{error}），請稍後再試。",
    },
    "line.no_pictures": {
        "en": "I found the pack but couldn't download any pictures from it.",
        "zh": "找到貼圖包，但無法下載任何圖片。",
    },
    "line.sticker_missing": {
        "en": "LINE says that sticker pack doesn't exist (or it's no longer for sale).",
        "zh": "LINE 表示這個貼圖包不存在（或已停售）。",
    },
    "line.sticker_unreadable": {
        "en": "LINE sent back something I couldn't read for that pack.",
        "zh": "無法讀取 LINE 傳回的貼圖包資料。",
    },
    "line.sticker_empty": {"en": "That sticker pack appears to be empty.", "zh": "這個貼圖包好像是空的。"},
    "line.emoji_missing": {
        "en": "LINE says that emoji pack doesn't exist (or it's no longer for sale).",
        "zh": "LINE 表示這個表情貼不存在（或已停售）。",
    },
    "line.emoji_unreadable": {
        "en": "LINE sent back something I couldn't read for that emoji pack.",
        "zh": "無法讀取 LINE 傳回的表情貼資料。",
    },
    "line.emoji_empty": {"en": "That emoji pack appears to be empty.", "zh": "這個表情貼好像是空的。"},
    # ---- Problems converting pictures ---------------------------------------------------
    "conv.unreadable_picture": {"en": "I couldn't read that picture.", "zh": "無法讀取這張圖片。"},
    "conv.too_detailed_static": {
        "en": "That picture is too detailed to fit Telegram's 512 KB limit.",
        "zh": "這張圖片太精細，無法壓縮到 Telegram 的 512 KB 上限。",
    },
    "conv.too_large": {"en": "That picture is far too large.", "zh": "這張圖片太大了。"},
    "conv.unreadable_file": {
        "en": "I couldn't read that file as a picture or a video.",
        "zh": "無法把這個檔案讀取為圖片或影片。",
    },
    "conv.unreadable_frames": {"en": "I couldn't read the frames of that video.", "zh": "無法讀取這段影片的畫面。"},
    "conv.tool_failed": {"en": "The video tool failed: {details}", "zh": "影片轉換工具出錯：{details}"},
    "conv.tool_stopped": {
        "en": "The video tool kept being stopped (code {code}) – please try again in a minute.",
        "zh": "影片轉換工具一直被中斷（代碼 {code}），請一分鐘後再試。",
    },
    "conv.too_detailed_video": {
        "en": "That animation is too detailed to fit Telegram's 256 KB limit (best try was {kb} KB).",
        "zh": "這個動畫太精細，無法壓縮到 Telegram 的 256 KB 上限（最佳結果為 {kb} KB）。",
    },
    "conv.tgs": {
        "en": "Animated .tgs stickers can't be converted (they're vector drawings, not pictures).",
        "zh": "無法轉換 .tgs 動態貼圖（它們是向量繪圖，不是圖片）。",
    },
    # ---- The / command menu in Telegram ------------------------------------------------------
    "cmd.start": {"en": "Welcome & help", "zh": "開始及使用說明"},
    "cmd.import": {"en": "Convert a LINE sticker or emoji pack", "zh": "轉換 LINE 貼圖或表情貼"},
    "cmd.manage": {"en": "Edit the sets I made for you", "zh": "管理我為你製作的貼圖包"},
    "cmd.download": {"en": "Get a ZIP of a LINE pack or Telegram set", "zh": "下載 LINE 或 Telegram 貼圖包的 ZIP"},
    "cmd.language": {"en": "中文 / English", "zh": "中文 / English"},
    "cmd.cancel": {"en": "Stop what we're doing", "zh": "取消目前的操作"},
}
