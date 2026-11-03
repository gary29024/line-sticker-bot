"""The bot's memory: which Telegram sticker sets it made, and for whom.

Telegram doesn't tell bots who owns a set, so the bot writes it down itself.
That's how "Manage my sets" knows which sets are yours.

There are two interchangeable "notebooks":

  * SqliteStorage - a database kept in one file on the server's disk.
    Used when the bot runs on Railway, Fly.io or your own computer.
    (Keep that file on a persistent disk / volume, or the bot forgets
    your sets every time it restarts.)

  * RemoteStorage - used on Cloudflare. A Cloudflare Container's disk is wiped
    whenever it goes to sleep, so the list is kept in Cloudflare's D1 database
    instead. The bot asks the Cloudflare Worker (worker/index.ts) to read and
    write it by calling http://store.worker/<action>.

Either way the sticker sets themselves live on Telegram and are never lost.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass

import httpx


@dataclass
class SetRecord:
    id: int
    name: str  # Telegram's short name, e.g. "ls11537_by_mybot"
    title: str
    owner_id: int
    sticker_type: str  # "regular" or "custom_emoji"
    source: str  # where it came from, e.g. a LINE link
    created_at: float

    @property
    def link(self) -> str:
        return set_link(self.name, self.sticker_type)


def set_link(name: str, sticker_type: str = "regular") -> str:
    kind = "addemoji" if sticker_type == "custom_emoji" else "addstickers"
    return f"https://t.me/{kind}/{name}"


def _record(data) -> SetRecord | None:
    if not data:
        return None
    data = dict(data)
    return SetRecord(
        id=int(data["id"]),
        name=data["name"],
        title=data["title"],
        owner_id=int(data["owner_id"]),
        sticker_type=data["sticker_type"],
        source=data.get("source") or "",
        created_at=float(data["created_at"]),
    )


SCHEMA = """CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    sticker_type TEXT NOT NULL DEFAULT 'regular',
    source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
)"""


class SqliteStorage:
    """The list kept in a file on this computer."""

    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.execute("CREATE INDEX IF NOT EXISTS sets_owner ON sets(owner_id)")
        self.db.commit()

    async def add_set(self, name: str, title: str, owner_id: int, sticker_type: str, source: str) -> SetRecord:
        self.db.execute(
            """INSERT INTO sets (name, title, owner_id, sticker_type, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET title=excluded.title, owner_id=excluded.owner_id,
                   sticker_type=excluded.sticker_type, source=excluded.source""",
            (name, title, owner_id, sticker_type, source, time.time()),
        )
        self.db.commit()
        return await self.get_by_name(name)

    async def get_by_name(self, name: str) -> SetRecord | None:
        return _record(self.db.execute("SELECT * FROM sets WHERE lower(name) = lower(?)", (name,)).fetchone())

    async def get_by_id(self, set_id: int) -> SetRecord | None:
        return _record(self.db.execute("SELECT * FROM sets WHERE id = ?", (int(set_id),)).fetchone())

    async def list_for_owner(self, owner_id: int) -> list[SetRecord]:
        rows = self.db.execute(
            "SELECT * FROM sets WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,)
        ).fetchall()
        return [_record(r) for r in rows]

    async def rename(self, name: str, title: str) -> None:
        self.db.execute("UPDATE sets SET title = ? WHERE lower(name) = lower(?)", (title, name))
        self.db.commit()

    async def delete(self, name: str) -> None:
        self.db.execute("DELETE FROM sets WHERE lower(name) = lower(?)", (name,))
        self.db.commit()

    async def close(self) -> None:
        self.db.close()


# Kept so older code and docs that say "Storage" still work.
Storage = SqliteStorage


class StorageError(Exception):
    pass


class RemoteStorage:
    """The list kept in Cloudflare D1, reached through the Cloudflare Worker.

    Every action is a small web request, e.g.
        POST http://store.worker/get_by_name   {"name": "ls11537_by_mybot"}
    and the Worker answers with {"result": ...}.
    """

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        # trust_env=False: talk to the Worker directly, never through a proxy.
        self.client = client or httpx.AsyncClient(timeout=20, trust_env=False)

    async def _call(self, action: str, **payload):
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self.client.post(f"{self.base_url}/{action}", json=payload)
                if resp.status_code >= 500:
                    raise StorageError(f"{resp.status_code} {resp.text[:200]}")
                resp.raise_for_status()
                return resp.json().get("result")
            except (httpx.HTTPError, StorageError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        raise StorageError(f"Couldn't reach the bot's database: {last_error}")

    async def add_set(self, name: str, title: str, owner_id: int, sticker_type: str, source: str) -> SetRecord:
        return _record(
            await self._call(
                "add_set", name=name, title=title, owner_id=owner_id, sticker_type=sticker_type, source=source
            )
        )

    async def get_by_name(self, name: str) -> SetRecord | None:
        return _record(await self._call("get_by_name", name=name))

    async def get_by_id(self, set_id: int) -> SetRecord | None:
        return _record(await self._call("get_by_id", id=int(set_id)))

    async def list_for_owner(self, owner_id: int) -> list[SetRecord]:
        return [_record(r) for r in (await self._call("list_for_owner", owner_id=owner_id) or [])]

    async def rename(self, name: str, title: str) -> None:
        await self._call("rename", name=name, title=title)

    async def delete(self, name: str) -> None:
        await self._call("delete", name=name)

    async def close(self) -> None:
        await self.client.aclose()
