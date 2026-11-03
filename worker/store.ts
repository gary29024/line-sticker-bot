/**
 * The bot's list of "who owns which sticker set", kept in Cloudflare D1.
 *
 * The Python bot inside the container can't talk to D1 directly, so it sends
 * small web requests to http://store.worker/<action>. Cloudflare catches those
 * requests before they leave the container and hands them to `handleStore`
 * below, which reads or writes D1 and answers with {"result": ...}.
 *
 * The actions match the Python class RemoteStorage in stickerbot/storage.py.
 */

export interface StoreEnv {
	DB: D1Database;
}

export interface SetRow {
	id: number;
	name: string;
	title: string;
	owner_id: number;
	sticker_type: string;
	source: string;
	created_at: number;
}

const SCHEMA = [
	`CREATE TABLE IF NOT EXISTS sets (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		name TEXT UNIQUE NOT NULL,
		title TEXT NOT NULL,
		owner_id INTEGER NOT NULL,
		sticker_type TEXT NOT NULL DEFAULT 'regular',
		source TEXT NOT NULL DEFAULT '',
		created_at REAL NOT NULL
	)`,
	`CREATE INDEX IF NOT EXISTS sets_owner ON sets(owner_id)`,
	// Each person's chosen language (only stored if they picked one with /language).
	`CREATE TABLE IF NOT EXISTS user_prefs (
		user_id INTEGER PRIMARY KEY,
		lang TEXT NOT NULL
	)`,
];

const LANGS = ["en", "zh"];

// The table is created automatically the first time it's needed - no setup step.
let schemaReady: Promise<unknown> | null = null;
function ensureSchema(db: D1Database): Promise<unknown> {
	if (!schemaReady) {
		schemaReady = db.batch(SCHEMA.map((sql) => db.prepare(sql))).catch((err) => {
			schemaReady = null; // try again next time
			throw err;
		});
	}
	return schemaReady;
}

class BadInput extends Error {}

function text(body: Record<string, unknown>, key: string, max = 512): string {
	const value = body[key];
	if (typeof value !== "string" || value.length === 0 || value.length > max) {
		throw new BadInput(`"${key}" must be a non-empty string`);
	}
	return value;
}

function int(body: Record<string, unknown>, key: string): number {
	const value = body[key];
	if (typeof value !== "number" || !Number.isSafeInteger(value)) {
		throw new BadInput(`"${key}" must be a whole number`);
	}
	return value;
}

const BY_NAME = "SELECT * FROM sets WHERE lower(name) = lower(?)";

async function run(db: D1Database, action: string, body: Record<string, unknown>): Promise<unknown> {
	switch (action) {
		case "add_set": {
			const name = text(body, "name", 64);
			const stickerType = text(body, "sticker_type", 32);
			if (stickerType !== "regular" && stickerType !== "custom_emoji") {
				throw new BadInput('"sticker_type" must be "regular" or "custom_emoji"');
			}
			await db
				.prepare(
					`INSERT INTO sets (name, title, owner_id, sticker_type, source, created_at)
					 VALUES (?, ?, ?, ?, ?, ?)
					 ON CONFLICT(name) DO UPDATE SET title = excluded.title, owner_id = excluded.owner_id,
					     sticker_type = excluded.sticker_type, source = excluded.source`,
				)
				.bind(name, text(body, "title", 128), int(body, "owner_id"), stickerType,
					typeof body.source === "string" ? body.source.slice(0, 512) : "", Date.now() / 1000)
				.run();
			return db.prepare(BY_NAME).bind(name).first<SetRow>();
		}
		case "get_by_name":
			return db.prepare(BY_NAME).bind(text(body, "name", 64)).first<SetRow>();
		case "get_by_id":
			return db.prepare("SELECT * FROM sets WHERE id = ?").bind(int(body, "id")).first<SetRow>();
		case "list_for_owner": {
			const { results } = await db
				.prepare("SELECT * FROM sets WHERE owner_id = ? ORDER BY created_at DESC")
				.bind(int(body, "owner_id"))
				.all<SetRow>();
			return results;
		}
		case "rename":
			await db
				.prepare("UPDATE sets SET title = ? WHERE lower(name) = lower(?)")
				.bind(text(body, "title", 128), text(body, "name", 64))
				.run();
			return null;
		case "delete":
			await db.prepare("DELETE FROM sets WHERE lower(name) = lower(?)").bind(text(body, "name", 64)).run();
			return null;
		case "get_lang": {
			const row = await db
				.prepare("SELECT lang FROM user_prefs WHERE user_id = ?")
				.bind(int(body, "user_id"))
				.first<{ lang: string }>();
			return row?.lang ?? null;
		}
		case "set_lang": {
			const userId = int(body, "user_id");
			if (body.lang === null) {
				await db.prepare("DELETE FROM user_prefs WHERE user_id = ?").bind(userId).run();
				return null;
			}
			const lang = text(body, "lang", 8);
			if (!LANGS.includes(lang)) throw new BadInput(`"lang" must be one of ${LANGS.join(", ")} or null`);
			await db
				.prepare(
					"INSERT INTO user_prefs (user_id, lang) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang",
				)
				.bind(userId, lang)
				.run();
			return null;
		}
		default:
			throw new BadInput(`unknown action "${action}"`);
	}
}

export async function handleStore(request: Request, env: StoreEnv): Promise<Response> {
	if (request.method !== "POST") {
		return Response.json({ error: "use POST" }, { status: 405 });
	}
	const action = new URL(request.url).pathname.replace(/^\/+/, "");
	let body: Record<string, unknown>;
	try {
		const parsed = await request.json();
		if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
		body = parsed as Record<string, unknown>;
	} catch {
		return Response.json({ error: "body must be a JSON object" }, { status: 400 });
	}
	try {
		await ensureSchema(env.DB);
		return Response.json({ result: (await run(env.DB, action, body)) ?? null });
	} catch (err) {
		if (err instanceof BadInput) {
			return Response.json({ error: err.message }, { status: 400 });
		}
		console.error("store error", action, err);
		return Response.json({ error: "database error" }, { status: 500 });
	}
}
