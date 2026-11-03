"""Settings, read from environment variables (the "control panel" of the bot).

On Railway / Fly.io you type these into the website's "Variables" or
"Secrets" page. When running on your own computer you can put them in a
file called .env (see .env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _load_dotenv(path: str = ".env") -> None:
    """Read KEY=value lines from a .env file, if there is one. Real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _id_list(value: str) -> frozenset[int]:
    return frozenset(int(x) for x in value.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit())


@dataclass(frozen=True)
class Config:
    bot_token: str
    data_dir: str
    admin_ids: frozenset[int]
    allowed_user_ids: frozenset[int]  # empty = anyone may use the bot
    default_emoji: str
    max_jobs: int  # how many imports/downloads may run at the same time
    convert_workers: int  # how many stickers to convert at the same time within one import
    log_level: str

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "stickerbot.sqlite3")

    def is_allowed(self, user_id: int) -> bool:
        return not self.allowed_user_ids or user_id in self.allowed_user_ids or user_id in self.admin_ids


def load() -> Config:
    _load_dotenv()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "BOT_TOKEN is missing. Get one from @BotFather on Telegram and set it as an environment variable."
        )
    return Config(
        bot_token=token,
        data_dir=os.environ.get("DATA_DIR", "data"),
        admin_ids=_id_list(os.environ.get("ADMIN_IDS", "")),
        allowed_user_ids=_id_list(os.environ.get("ALLOWED_USER_IDS", "")),
        default_emoji=os.environ.get("DEFAULT_EMOJI", "⭐").strip() or "⭐",
        max_jobs=max(1, int(os.environ.get("MAX_JOBS", "2"))),
        convert_workers=max(1, int(os.environ.get("CONVERT_WORKERS", "2"))),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
