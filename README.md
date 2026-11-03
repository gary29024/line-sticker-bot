# LINE → Telegram Sticker Bot

A Telegram bot that turns **LINE stickers and emoji** into **Telegram sticker sets**.
Send it a LINE store link and, about a minute later, you get a Telegram sticker set you can add with one tap.

Works with:

| LINE pack type | Becomes on Telegram |
| --- | --- |
| Still stickers | Still stickers (PNG, 512 px) |
| Animated stickers | Video stickers (WEBM, 512 px, ≤ 3 s, see-through background kept) |
| Pop-up / effect stickers | Video stickers made from the pop-up animation |
| Sound stickers | Animated stickers *without* sound (Telegram stickers are silent) |
| Still or animated LINE emoji | A Telegram **custom emoji** set (100 × 100 px) |

Other features:

- **Manage your sets** – add pictures, GIFs, videos or stickers; delete, move or re-tag single stickers; rename or delete a set.
- **Download as ZIP** – get the original files of any LINE pack, or every sticker in any Telegram set.
- **No duplicate work** – if someone already converted a pack, the bot gives you the existing set straight away, and offers to make you your own editable copy.
- **Private mode** – optionally limit the bot to you and your friends.

> Inspired by [moe-sticker-bot](https://github.com/star-39/moe-sticker-bot). This is an independent, smaller rewrite in Python, made to be easy to read and host.

---

## How it works (in plain words)

```
 You send a LINE link
        │
        ▼
 1. line_store.py   asks LINE's public file server for the pack's details and downloads the pictures
        │
        ▼
 2. convert.py      resizes each picture to Telegram's size rules; animated ones become small videos
        │
        ▼
 3. jobs.py         creates the Telegram sticker set and uploads the stickers one by one,
        │           showing a progress bar and waiting politely when Telegram says "slow down"
        ▼
 4. storage.py      writes down that the set is yours, so you can edit it later with /manage
```

`bot.py` is the "conversation" part: what the bot says, its buttons, and what it does with each message.

---

## Step 1 – Create your bot on Telegram (2 minutes)

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Choose a display name (e.g. *My Sticker Maker*) and a username ending in `bot` (e.g. `my_sticker_maker_bot`).
3. BotFather replies with a **token** that looks like `123456789:AAH...`. Keep it secret – anyone with it controls your bot.

> Tip: keep the bot's username short. Every set name must end in `_by_<bot username>` and is limited to 64 characters.

---

## Step 2 – Put the bot online

A bot only answers while its program is running, so it needs a computer that's always on.
Free hosting plans usually put programs to sleep, so budget roughly **US$5–7 per month**.
Check each provider's current pricing page.

### Option A: Railway (easiest – everything happens in your web browser)

1. Sign up at [railway.com](https://railway.com) with your GitHub account.
2. **New Project → Deploy from GitHub repo →** pick this repository.
   Railway finds the `Dockerfile` and builds the bot automatically.
3. Open the service → **Variables** → add `BOT_TOKEN` = *your token from BotFather*.
   (Optional: add `ALLOWED_USER_IDS` to make it private – see *Settings* below.)
4. **Add a volume** so the bot remembers your sets across restarts:
   right-click the service (or press ⌘K / Ctrl+K) → **Add Volume** → mount path **`/data`**.
5. Railway restarts the bot. Open your bot in Telegram and send `/start`. 🎉

Keep it at **one replica**. Two copies of the same bot fight over messages. `railway.json` already sets this.

### Option B: Fly.io (uses a command-line tool)

```bash
# install flyctl: https://fly.io/docs/flyctl/install/
fly auth signup                         # or: fly auth login
# edit fly.toml: change "app" to a unique name
fly launch --no-deploy --copy-config
fly volumes create stickerbot_data --size 1
fly secrets set BOT_TOKEN=123456789:AAH...
fly deploy
fly logs                                # see what the bot is doing
```

### Option C: Your own computer or server (with Docker)

```bash
docker build -t line-sticker-bot .
docker run -d --restart unless-stopped --name stickerbot \
  -e BOT_TOKEN=123456789:AAH... \
  -v stickerbot-data:/data \
  line-sticker-bot
```

### Option D: Your own computer (without Docker)

Needs Python 3.11+ and ffmpeg (`brew install ffmpeg` on Mac, `sudo apt install ffmpeg` on Ubuntu/Debian).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then put your token in .env
python -m stickerbot
```

---

## Settings

Set these as environment variables (Railway: *Variables*, Fly.io: `fly secrets set`, or the `.env` file locally).

| Name | Needed? | What it does |
| --- | --- | --- |
| `BOT_TOKEN` | **yes** | The token from @BotFather. |
| `ALLOWED_USER_IDS` | no | Comma-separated Telegram user IDs allowed to use the bot. Empty = everyone. Send `/myid` to the bot to see your ID. |
| `ADMIN_IDS` | no | User IDs who may edit **any** set the bot made (handy if someone loses access). |
| `DATA_DIR` | no | Folder for the bot's small database. Default `data` (`/data` in Docker). |
| `DEFAULT_EMOJI` | no | Emoji used when you tap "Use ⭐ for all". Default ⭐. |
| `MAX_JOBS` | no | How many packs can be converted at the same time across all users. Default 2. |
| `CONVERT_WORKERS` | no | How many stickers in one pack are converted at the same time. Default 2. Raise both on a bigger server. |

---

## Using the bot

| You send… | The bot… |
| --- | --- |
| a LINE sticker/emoji link | shows the pack and offers **Make a Telegram set** or **Download originals (ZIP)** |
| `/import` | asks for a LINE link and goes straight to making a set |
| `/manage` | lists your sets → add stickers, rename, download, delete |
| any sticker from one of your sets | offers **Delete**, **Move**, **Change emoji** for that sticker |
| any other sticker, or a `t.me/addstickers/...` link | offers to download the whole set as a ZIP |
| `/download` | asks what to download, then sends the ZIP |
| `/cancel` | stops whatever you were doing |
| `/myid` | tells you your Telegram user ID |

Making a set takes about three questions: the set's name (or keep LINE's), which emoji to tag the stickers with, and that's it.
A 40-sticker animated pack takes roughly 1–2 minutes on a small server.

Links the bot understands:

- `https://store.line.me/stickershop/product/11537/en`
- `https://line.me/S/sticker/11537` (the LINE app's *Share* link)
- `https://store.line.me/emojishop/product/5ad6f0f7031a67c6648c6432/en`
- `https://line.me/S/emoji/?id=5ad6f0f7031a67c6648c6432`

---

## Good to know

- **Telegram's rules** for sticker files: still = PNG/WEBP, one side exactly 512 px, ≤ 512 KB. Video = VP9 WEBM, ≤ 3 s, ≤ 30 frames/s, ≤ 256 KB, no sound. Custom emoji = 100 × 100 px. The bot does all of this for you. Animations longer than 3 s are sped up slightly to fit.
- **Custom emoji** (from LINE emoji packs) can be *seen* by everyone, but only Telegram Premium users can *send* them.
- **Custom-text stickers** (LINE packs where you type your own words) come through without the text.
- Sets made by a bot can only be edited **through that bot**, so keep it running if you want to keep editing.
- If the bot's database is lost (no volume attached), your sets still exist on Telegram, but `/manage` won't list them any more.
- LINE stickers are copyrighted by their creators. Use converted sets for personal use.

---

## Code tour

| File | What it does |
| --- | --- |
| `stickerbot/__main__.py` | Starts the bot (`python -m stickerbot`). |
| `stickerbot/config.py` | Reads the settings above. |
| `stickerbot/line_store.py` | Understands LINE links, reads pack details, downloads the pictures. Knows where LINE keeps still, animated, pop-up and emoji files. |
| `stickerbot/convert.py` | Turns any picture/animation/video into a Telegram-ready sticker. Uses Pillow for pictures and ffmpeg for video. Tries lower qualities until the file is small enough. |
| `stickerbot/telegram_sets.py` | Telegram set names, polite retrying ("flood control"), adding stickers without accidental duplicates, ZIP of a Telegram set. |
| `stickerbot/jobs.py` | The slow work (import, ZIP downloads) with a live progress message. |
| `stickerbot/storage.py` | A tiny SQLite database: which set belongs to whom. |
| `stickerbot/bot.py` | Everything the bot says and every button. |
| `tests/` | Automatic checks, including a pretend Telegram server so the whole conversation can be tested without a real bot. |

### Running the tests

```bash
pip install -r requirements-dev.txt
pytest                              # offline tests (about 15 seconds)
RUN_NETWORK_TESTS=1 pytest          # also download real packs from LINE
```
