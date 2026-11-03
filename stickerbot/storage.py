"""The bot's memory: which Telegram sticker sets it made, and for whom.

Telegram doesn't tell bots who owns a set, so the bot writes it down itself in
a small database file (SQLite - a whole database in one file, no server needed).
That's how "Manage my sets" knows which sets are yours.

Keep this file on a persistent disk ("volume") when hosting, or the bot will
forget your sets every time it restarts. The sets themselves stay on Telegram
either way.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass


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


class Storage:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS sets (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT UNIQUE NOT NULL,
                   title TEXT NOT NULL,
                   owner_id INTEGER NOT NULL,
                   sticker_type TEXT NOT NULL DEFAULT 'regular',
                   source TEXT NOT NULL DEFAULT '',
                   created_at REAL NOT NULL
               )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS sets_owner ON sets(owner_id)")
        self.db.commit()

    @staticmethod
    def _record(row: sqlite3.Row | None) -> SetRecord | None:
        return SetRecord(**dict(row)) if row else None

    def add_set(self, name: str, title: str, owner_id: int, sticker_type: str, source: str) -> SetRecord:
        self.db.execute(
            """INSERT INTO sets (name, title, owner_id, sticker_type, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET title=excluded.title, owner_id=excluded.owner_id,
                   sticker_type=excluded.sticker_type, source=excluded.source""",
            (name, title, owner_id, sticker_type, source, time.time()),
        )
        self.db.commit()
        return self.get_by_name(name)

    def get_by_name(self, name: str) -> SetRecord | None:
        row = self.db.execute("SELECT * FROM sets WHERE lower(name) = lower(?)", (name,)).fetchone()
        return self._record(row)

    def get_by_id(self, set_id: int) -> SetRecord | None:
        return self._record(self.db.execute("SELECT * FROM sets WHERE id = ?", (set_id,)).fetchone())

    def list_for_owner(self, owner_id: int) -> list[SetRecord]:
        rows = self.db.execute(
            "SELECT * FROM sets WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,)
        ).fetchall()
        return [self._record(r) for r in rows]

    def rename(self, name: str, title: str) -> None:
        self.db.execute("UPDATE sets SET title = ? WHERE lower(name) = lower(?)", (title, name))
        self.db.commit()

    def delete(self, name: str) -> None:
        self.db.execute("DELETE FROM sets WHERE lower(name) = lower(?)", (name,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
