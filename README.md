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
- **Runs on Cloudflare** (Worker + Container, sleeps when idle), or on Railway, Fly.io or any computer with Docker.

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

### On Cloudflare

```
 Telegram ──(new message)──▶ Cloudflare Worker (worker/index.ts)
                                │  checks Telegram's secret password
                                ▼
                             Cloudflare Container  ── the same Python bot, woken up on demand,
                                │                    asleep after 10 quiet minutes
                                ▼
                             D1 database (worker/store.ts) ── the list of who owns which set
```

A Worker on its own can't do the job: animated stickers need ffmpeg (a video tool), and Workers can't run
programs like that or spend minutes converting. So the Worker is only the front desk. The real work
happens in a Container, a small Linux computer that Cloudflare starts when a message arrives.

---

## Step 1 – Create your bot on Telegram (2 minutes)

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Choose a display name (e.g. *My Sticker Maker*) and a username ending in `bot` (e.g. `my_sticker_maker_bot`).
3. BotFather replies with a **token** that looks like `123456789:AAH...`. Keep it secret – anyone with it controls your bot.

> Tip: keep the bot's username short. Every set name must end in `_by_<bot username>` and is limited to 64 characters.

---

## Step 2 – Put the bot online

### Option A: Cloudflare (recommended if you already pay for Workers)

**What you need:** a Cloudflare account on the **Workers Paid** plan (US$5/month), which includes
Containers. A personal bot normally stays inside the included usage. It only runs while it's awake:
about 10 minutes after the last message, or until a conversion finishes.

**1. Make a secret password for the webhook.**
Make up a random password of 32+ letters and numbers (a password manager is ideal). Only letters, numbers,
`_` and `-` are allowed. This is your `WEBHOOK_SECRET`. Telegram sends it with every message, so the bot
knows the message really came from Telegram.

**2. Connect this GitHub repository to Cloudflare.**
1. Open the [Cloudflare dashboard](https://dash.cloudflare.com) → **Workers & Pages** → **Create application**.
2. Choose **Import a repository** / **Continue with GitHub**, and pick this repository.
3. Keep the project name **`line-sticker-bot`**. It must match `name` in `wrangler.jsonc`.
4. Leave the build command empty and the deploy command as `npx wrangler deploy`. Click **Save and Deploy**.
5. The first build takes 5–10 minutes, because Cloudflare builds the bot's container.
   The D1 database is created automatically.

From now on, every change pushed to the `main` branch on GitHub is deployed automatically.

**3. Add the two secrets.**
Your Worker → **Settings** → **Variables and Secrets** → **Add**, choose type **Secret**, and add:

| Name | Value |
| --- | --- |
| `BOT_TOKEN` | the token from @BotFather |
| `WEBHOOK_SECRET` | the password from step 1 |

Click **Deploy** to save.

**4. Connect Telegram to the Worker (once).**
Open this address in your browser, using your own Worker address (shown on the Worker's page) and your password:

```
https://line-sticker-bot.<your-subdomain>.workers.dev/setup?key=<WEBHOOK_SECRET>
```

You should see **✅ Connected**.

**5. Try it.** Send `/start` to your bot in Telegram. The first reply can take up to a minute while Cloudflare
wakes the bot. After that it answers quickly until it goes back to sleep.

**Optional settings** such as making the bot private live in the `vars` section of `wrangler.jsonc`.
Edit them on GitHub, and Cloudflare redeploys automatically. See *Settings* below.

**Speed vs. cost:** `wrangler.jsonc` uses `"instance_type": "standard-1"` (½ CPU, 4 GB). A 40-sticker
animated pack takes about 2 minutes. `"standard-2"` (1 CPU) is about twice as fast and uses a bit more
of the included memory allowance. See [Containers pricing](https://developers.cloudflare.com/containers/pricing/).

<details>
<summary>If the first build says it couldn't create the D1 database</summary>

In the dashboard go to **Storage & Databases → D1 → Create**, name it `line-sticker-bot`, and copy its
**Database ID**. In `wrangler.jsonc`, change the `d1_databases` line to
`[{ "binding": "DB", "database_name": "line-sticker-bot", "database_id": "<the ID>" }]`, then push.
</details>

<details>
<summary>Watching what the bot does</summary>

Your Worker → **Logs** shows the Worker's activity. **Workers & Pages → Containers** shows whether the bot's
container is awake, plus its logs.
</details>

### Option B: Railway (always on, about US$5–7/month)

1. Sign up at [railway.com](https://railway.com) with your GitHub account.
2. **New Project → Deploy from GitHub repo →** pick this repository.
   Railway finds the `Dockerfile` and builds the bot automatically.
3. Open the service → **Variables** → add `BOT_TOKEN` = *your token from BotFather*.
   (Optional: add `ALLOWED_USER_IDS` to make it private – see *Settings* below.)
4. **Add a volume** so the bot remembers your sets across restarts:
   right-click the service (or press ⌘K / Ctrl+K) → **Add Volume** → mount path **`/data`**.
5. Railway restarts the bot. Open your bot in Telegram and send `/start`. 🎉

Keep it at **one replica**. Two copies of the same bot fight over messages. `railway.json` already sets this.

### Option C: Fly.io (uses a command-line tool)

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

### Option D: Your own computer or server (with Docker)

```bash
docker build -t line-sticker-bot .
docker run -d --restart unless-stopped --name stickerbot \
  -e BOT_TOKEN=123456789:AAH... \
  -v stickerbot-data:/data \
  line-sticker-bot
```

### Option E: Your own computer (without Docker)

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

Where to set them:
- **Cloudflare:** `BOT_TOKEN` and `WEBHOOK_SECRET` as Secrets in the dashboard. Everything else goes in the `vars` section of `wrangler.jsonc`.
- **Railway:** *Variables*. **Fly.io:** `fly secrets set`. **Your computer:** the `.env` file.

| Name | Needed? | What it does |
| --- | --- | --- |
| `BOT_TOKEN` | **yes** | The token from @BotFather. |
| `WEBHOOK_SECRET` | Cloudflare only | The password Telegram sends with each message (see Option A). |
| `ALLOWED_USER_IDS` | no | Comma-separated Telegram user IDs allowed to use the bot. Empty = everyone. Send `/myid` to the bot to see your ID. |
| `ADMIN_IDS` | no | User IDs who may edit **any** set the bot made (handy if someone loses access). |
| `DATA_DIR` | no | Folder for the bot's small database. Default `data` (`/data` in Docker). Not used on Cloudflare (D1 instead). |
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
- If the bot's database is lost (for example, no volume attached on Railway), your sets still exist on Telegram, but `/manage` won't list them any more. On Cloudflare the list lives in D1, which is kept safely.
- On Cloudflare the bot sleeps after 10 quiet minutes. If you leave a half-finished question (like "what should the set be called?") for longer than that, just send the LINE link again.
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
| `stickerbot/storage.py` | Which set belongs to whom: a small database file, or Cloudflare D1. |
| `stickerbot/bot.py` | Everything the bot says and every button. |
| `stickerbot/webhook.py` | Cloudflare only: the small web server that receives messages from the Worker. |
| `worker/index.ts` | Cloudflare only: the Worker "front desk" – checks Telegram's password, wakes the container, the `/setup` page. |
| `worker/store.ts` | Cloudflare only: reads and writes the list of sets in the D1 database. |
| `wrangler.jsonc` | Cloudflare settings: container size, database, optional settings. |
| `tests/` | Automatic checks, including a pretend Telegram server so the whole conversation can be tested without a real bot. |

### Running the tests

```bash
pip install -r requirements-dev.txt
npm install                         # Cloudflare tools (lets the tests run the Worker code locally)
pytest                              # offline tests (about 25 seconds)
RUN_NETWORK_TESTS=1 pytest          # also download real packs from LINE
```
