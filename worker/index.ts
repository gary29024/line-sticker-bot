/**
 * The Cloudflare "front desk" for the sticker bot.
 *
 * In plain words:
 *  1. Telegram sends every new message to this Worker's web address
 *     (https://<your-worker>.workers.dev/telegram). This is called a "webhook".
 *  2. The Worker checks the secret password Telegram attaches (WEBHOOK_SECRET),
 *     so strangers can't pretend to be Telegram.
 *  3. It passes the message to the real bot - the Python program in this repo -
 *     which runs inside a Cloudflare Container: a small Linux computer that
 *     Cloudflare starts when a message arrives and puts to sleep after 10 quiet
 *     minutes (so you only pay while it's awake).
 *  4. The bot remembers "who owns which set" in a D1 database. It reaches D1 by
 *     calling http://store.worker, which Cloudflare routes to worker/store.ts.
 *
 * One-time setup: open https://<your-worker>.workers.dev/setup?key=<WEBHOOK_SECRET>
 * in a browser. That tells Telegram where to send messages.
 */

import { Container, getContainer } from "@cloudflare/containers";
import { handleStore } from "./store";

// Required by Cloudflare so the container's calls to http://store.worker can be caught.
export { ContainerProxy } from "@cloudflare/containers";

export interface Env {
	STICKER_BOT: DurableObjectNamespace<StickerBot>;
	DB: D1Database;
	// Secrets (Cloudflare dashboard → your Worker → Settings → Variables and Secrets)
	BOT_TOKEN: string;
	WEBHOOK_SECRET: string;
	// Optional settings (the "vars" section of wrangler.jsonc)
	ALLOWED_USER_IDS?: string;
	ADMIN_IDS?: string;
	DEFAULT_EMOJI?: string;
	MAX_JOBS?: string;
	CONVERT_WORKERS?: string;
	LOG_LEVEL?: string;
}

const STORE_HOST = "store.worker";
const CONTAINER_PORT = 8080;
const WAKE_TIMEOUT_MS = 90_000;
const STARTING_UP_TEXT =
	"⏳ The bot is starting up on Cloudflare – this can take a few minutes right after an update. " +
	"Please send that again in a minute.";

export class StickerBot extends Container<Env> {
	defaultPort = CONTAINER_PORT;
	// Go to sleep after 10 minutes without messages (unless a pack is still converting).
	sleepAfter = "10m";

	constructor(ctx: DurableObjectState<{}>, env: Env) {
		super(ctx, env);
		// Settings handed to the Python bot when its container starts.
		this.envVars = {
			MODE: "webhook",
			PORT: String(CONTAINER_PORT),
			STORE_URL: `http://${STORE_HOST}`,
			DATA_DIR: "/tmp",
			BOT_TOKEN: env.BOT_TOKEN ?? "",
			WEBHOOK_SECRET: env.WEBHOOK_SECRET ?? "",
			ALLOWED_USER_IDS: env.ALLOWED_USER_IDS ?? "",
			ADMIN_IDS: env.ADMIN_IDS ?? "",
			DEFAULT_EMOJI: env.DEFAULT_EMOJI ?? "",
			MAX_JOBS: env.MAX_JOBS ?? "",
			CONVERT_WORKERS: env.CONVERT_WORKERS ?? "",
			LOG_LEVEL: env.LOG_LEVEL ?? "INFO",
		};
	}

	/**
	 * Make sure the bot is running, starting it if it's asleep.
	 *
	 * The Containers library normally gives Cloudflare only 8 seconds to provide a
	 * container, which isn't always enough right after a deploy (you'd see "there is no
	 * container instance that can be provided to this Durable Object"). Allow much longer.
	 */
	async wake(): Promise<void> {
		const state = await this.getState();
		if (this.ctx.container?.running && state.status === "healthy") return;
		await this.startAndWaitForPorts({
			ports: CONTAINER_PORT,
			cancellationOptions: { instanceGetTimeoutMS: WAKE_TIMEOUT_MS, portReadyTimeoutMS: WAKE_TIMEOUT_MS + 30_000 },
		});
	}

	/** Called when the bot has been quiet for `sleepAfter`. Don't fall asleep mid-conversion. */
	override async onActivityExpired(): Promise<void> {
		try {
			const res = await this.containerFetch("http://container/health", {}, CONTAINER_PORT);
			if (res.ok) {
				const health = (await res.json()) as { busy?: number };
				if ((health.busy ?? 0) > 0) {
					console.log(`Still converting (${health.busy} job(s)) - staying awake`);
					return; // the timer restarts and we'll check again later
				}
			}
		} catch (err) {
			console.log("Health check failed, stopping container", err);
		}
		await this.stop();
	}
}

// Calls from the container to http://store.worker go to the D1 code in store.ts.
// (Assigned like this, not as a class field, so the Containers library registers it.)
StickerBot.outboundByHost = {
	[STORE_HOST]: (request: Request, env: Env) => handleStore(request, env),
};

/** Compare two secrets without leaking how much of them matched. */
function sameSecret(given: string | null, expected: string | undefined): boolean {
	if (!given || !expected) return false;
	const a = new TextEncoder().encode(given);
	const b = new TextEncoder().encode(expected);
	return a.byteLength === b.byteLength && crypto.subtle.timingSafeEqual(a, b);
}

function page(title: string, body: string, status = 200): Response {
	const html = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>${title}</title><body style="font-family:system-ui;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5">
<h1>${title}</h1>${body}</body>`;
	return new Response(html, { status, headers: { "content-type": "text/html; charset=utf-8" } });
}

function escapeHtml(value: string): string {
	return value.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}

type TelegramReply = { ok: boolean; description?: string; result?: unknown };

async function telegram(env: Env, method: string, payload: unknown): Promise<TelegramReply> {
	try {
		const res = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify(payload ?? {}),
			signal: AbortSignal.timeout(10_000),
		});
		return (await res.json()) as TelegramReply;
	} catch (err) {
		return { ok: false, description: `Couldn't reach Telegram: ${String(err)}` };
	}
}

/** The chat a Telegram update came from (a message, or a button tap on a message). */
function chatIdOf(update: unknown): number | undefined {
	const u = update as {
		message?: { chat?: { id?: number } };
		callback_query?: { message?: { chat?: { id?: number } } };
	};
	return u?.message?.chat?.id ?? u?.callback_query?.message?.chat?.id;
}

/** Pass one Telegram update to the bot, waking it first. Gives back the bot's answer, or what went wrong. */
async function deliver(env: Env, body: string): Promise<{ response?: Response; problem?: string }> {
	const bot = getContainer(env.STICKER_BOT, "main");
	try {
		await bot.wake();
		const response = await bot.fetch(
			new Request("http://container/telegram", {
				method: "POST",
				headers: { "content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": env.WEBHOOK_SECRET },
				body,
			}),
		);
		if (response.status < 500) return { response };
		return { problem: `container answered ${response.status}: ${(await response.text()).slice(0, 300)}` };
	} catch (err) {
		return { problem: err instanceof Error ? err.message : String(err) };
	}
}

/** A page for checking on the bot: is its container running? Optionally wake it and show any error. */
async function status(url: URL, env: Env): Promise<Response> {
	if (!sameSecret(url.searchParams.get("key"), env.WEBHOOK_SECRET)) {
		return page("Wrong key", "<p>Add <code>?key=</code> followed by your WEBHOOK_SECRET to the address.</p>", 403);
	}
	const bot = getContainer(env.STICKER_BOT, "main");
	let wakeResult = "";
	if (url.searchParams.get("wake") === "1") {
		const started = Date.now();
		try {
			await bot.wake();
			wakeResult = `<p>✅ The bot is awake (took ${Math.round((Date.now() - started) / 1000)} s).</p>`;
		} catch (err) {
			wakeResult = `<p>❌ Couldn't start the bot after ${Math.round((Date.now() - started) / 1000)} s:</p>
<pre style="white-space:pre-wrap">${escapeHtml(err instanceof Error ? err.message : String(err))}</pre>`;
		}
	}
	let state = "unknown";
	try {
		state = (await bot.getState()).status;
	} catch (err) {
		state = `couldn't check (${escapeHtml(String(err))})`;
	}
	const info = await telegram(env, "getWebhookInfo", {});
	const hook = info.result as { url?: string; pending_update_count?: number; last_error_message?: string } | undefined;
	const meaning: Record<string, string> = {
		healthy: "running and ready",
		running: "starting up",
		stopping: "going to sleep",
		stopped: "asleep (it wakes up when a message arrives)",
		stopped_with_code: "stopped after an error – see the Containers logs",
	};
	const wakeLink = `${url.origin}/status?key=${encodeURIComponent(url.searchParams.get("key") ?? "")}&wake=1`;
	return page(
		"Bot status",
		`${wakeResult}
<p><b>Container:</b> ${escapeHtml(state)} – ${escapeHtml(meaning[state] ?? "")}</p>
<p><b>Telegram webhook:</b> ${escapeHtml(info.ok ? hook?.url || "not set – open /setup first" : `couldn't check (${info.description ?? "no answer"})`)}<br>
<b>Messages waiting:</b> ${hook?.pending_update_count ?? "?"}<br>
<b>Telegram's last delivery error:</b> ${escapeHtml(hook?.last_error_message || "none")}</p>
<p><a href="${escapeHtml(wakeLink)}">Wake the bot now</a> (can take up to a couple of minutes right after an update).</p>`,
	);
}

/** One-time setup: tell Telegram to send messages to this Worker. */
async function setup(url: URL, env: Env): Promise<Response> {
	if (!env.BOT_TOKEN || !env.WEBHOOK_SECRET) {
		return page(
			"Almost there",
			"<p>Add the secrets <b>BOT_TOKEN</b> and <b>WEBHOOK_SECRET</b> to this Worker first " +
				"(Cloudflare dashboard → your Worker → Settings → Variables and Secrets), then reload this page.</p>",
			500,
		);
	}
	if (!/^[A-Za-z0-9_-]{16,256}$/.test(env.WEBHOOK_SECRET)) {
		return page(
			"WEBHOOK_SECRET needs changing",
			"<p>It must be 16–256 characters using only letters, numbers, <code>_</code> and <code>-</code>.</p>",
			500,
		);
	}
	if (!sameSecret(url.searchParams.get("key"), env.WEBHOOK_SECRET)) {
		return page("Wrong key", "<p>Add <code>?key=</code> followed by your WEBHOOK_SECRET to the address.</p>", 403);
	}
	const hook = `${url.origin}/telegram`;
	const result = await telegram(env, "setWebhook", {
		url: hook,
		secret_token: env.WEBHOOK_SECRET,
		allowed_updates: ["message", "callback_query"],
		max_connections: 10,
	});
	if (!result.ok) {
		return page("Telegram said no", `<p>${escapeHtml(result.description ?? "Unknown error")}</p>
<p>Check that BOT_TOKEN is exactly the token from @BotFather.</p>`, 502);
	}
	const me = (await telegram(env, "getMe", {})).result as { username?: string } | undefined;
	const name = me?.username ? `@${escapeHtml(me.username)}` : "your bot";
	return page(
		"✅ Connected",
		`<p>Telegram will now send ${name}'s messages to <code>${escapeHtml(hook)}</code>.</p>
<p>Open ${name} in Telegram and send <b>/start</b>. The very first reply can take up to a minute while
Cloudflare starts the bot; after that it's quick until it goes to sleep again.</p>`,
	);
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);

		if (url.pathname === "/telegram") {
			if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });
			if (!sameSecret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), env.WEBHOOK_SECRET)) {
				return new Response("Forbidden", { status: 403 });
			}
			// Hand the message to the bot, waking its container if it's asleep.
			const body = await request.text();
			const { response, problem } = await deliver(env, body);
			if (response) return response;

			// The container couldn't be started. Tell the person (instead of silence), and answer
			// Telegram "OK" so it doesn't keep re-sending the same message.
			console.error("Bot container unavailable:", problem);
			let chatId: number | undefined;
			try {
				chatId = chatIdOf(JSON.parse(body));
			} catch {
				chatId = undefined;
			}
			if (chatId !== undefined) {
				await telegram(env, "sendMessage", { chat_id: chatId, text: STARTING_UP_TEXT });
			}
			return new Response("bot unavailable; user notified");
		}

		if (url.pathname === "/status") {
			return status(url, env);
		}

		if (url.pathname === "/setup") {
			return setup(url, env);
		}

		if (url.pathname === "/") {
			return page(
				"LINE → Telegram sticker bot",
				"<p>This Worker is running. If you haven't yet, open <code>/setup?key=YOUR_WEBHOOK_SECRET</code> once " +
					"to connect it to Telegram.</p>",
			);
		}

		return new Response("Not found", { status: 404 });
	},
} satisfies ExportedHandler<Env>;
