"""A pretend Telegram server, so the bot can be tested without a real bot token.

It answers the Bot API methods the bot uses and remembers sticker sets in memory.
"""

from __future__ import annotations

import itertools
import json
import time
from collections import defaultdict

from telegram.request import BaseRequest, RequestData

BOT_USER = {"id": 999, "is_bot": True, "first_name": "Sticker Bot", "username": "test_sticker_bot"}


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.sets: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.ids = itertools.count(1000)
        self.flood_once: set[str] = set()  # methods that answer "429 retry later" once
        self.sent_documents: list[tuple[str, bytes]] = []

    # --- BaseRequest plumbing -------------------------------------------------
    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self):
        return 5

    async def do_request(self, url, method, request_data: RequestData | None = None, **_):
        if "/file/bot" in url:
            path = url.split("/file/bot", 1)[1].split("/", 1)[1]
            return 200, self.files[path]
        api_method = url.rsplit("/", 1)[1]
        params = {}
        files = {}
        if request_data is not None:
            for key, value in (request_data.json_parameters or {}).items():
                try:
                    params[key] = json.loads(value)
                except (TypeError, ValueError):
                    params[key] = value
            files = request_data.multipart_data or {}
        self.calls.append((api_method, params))
        if api_method in self.flood_once:
            self.flood_once.discard(api_method)
            return 429, json.dumps(
                {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1",
                 "parameters": {"retry_after": 1}}
            ).encode()
        handler = getattr(self, "m_" + api_method, None)
        if handler is None:
            return self._ok(True)
        return handler(params, files)

    # --- helpers --------------------------------------------------------------
    @staticmethod
    def _ok(result):
        return 200, json.dumps({"ok": True, "result": result}).encode()

    @staticmethod
    def _bad(description):
        return 400, json.dumps({"ok": False, "error_code": 400, "description": f"Bad Request: {description}"}).encode()

    def _message(self, params, **extra):
        msg = {
            "message_id": next(self.ids),
            "date": int(time.time()),
            "chat": {"id": params.get("chat_id", 1), "type": "private"},
            "from": BOT_USER,
        }
        msg.update(extra)
        return msg

    def called(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]

    def texts(self) -> list[str]:
        return [p.get("text", "") for m, p in self.calls if m in ("sendMessage", "editMessageText")]

    def last_markup(self) -> list[list[dict]]:
        for m, p in reversed(self.calls):
            if m == "sendMessage" and p.get("reply_markup"):
                return p["reply_markup"]["inline_keyboard"]
        return []

    def _store_sticker(self, set_name, item, files):
        ref = item["sticker"]
        if isinstance(ref, str) and ref.startswith("attach://"):
            filename, data, *_ = files[ref[len("attach://"):]]
        else:  # an existing file_id
            filename, data = "reused", self.files.get(ref, b"")
        file_id = f"F{next(self.ids)}"
        self.files[file_id] = data
        return {
            "file_id": file_id,
            "file_unique_id": "U" + file_id,
            "type": self.sets[set_name]["sticker_type"],
            "width": 512,
            "height": 512,
            "is_animated": item["format"] == "animated",
            "is_video": item["format"] == "video",
            "emoji": item["emoji_list"][0],
            "set_name": set_name,
            "_format": item["format"],
            "_bytes": len(data),
        }

    def _public_set(self, s):
        stickers = [{k: v for k, v in st.items() if not k.startswith("_")} for st in s["stickers"]]
        return {"name": s["name"], "title": s["title"], "sticker_type": s["sticker_type"], "stickers": stickers}

    # --- Bot API methods ------------------------------------------------------
    def m_getMe(self, params, files):
        return self._ok(BOT_USER | {"can_join_groups": True, "can_read_all_group_messages": False,
                                    "supports_inline_queries": False})

    def m_sendMessage(self, params, files):
        return self._ok(self._message(params, text=params.get("text", "")))

    def m_editMessageText(self, params, files):
        return self._ok(self._message(params, text=params.get("text", "")))

    def m_sendDocument(self, params, files):
        filename, data, *_ = files["document"]
        self.sent_documents.append((filename, data))
        return self._ok(self._message(params, document={"file_id": "D1", "file_unique_id": "D1u", "file_name": filename}))

    def m_getStickerSet(self, params, files):
        s = self.sets.get(params["name"].lower())
        return self._ok(self._public_set(s)) if s else self._bad("STICKERSET_INVALID")

    def m_createNewStickerSet(self, params, files):
        name = params["name"]
        if name.lower() in self.sets:
            return self._bad("sticker set name is already occupied")
        if not name.endswith("_by_" + BOT_USER["username"]):
            return self._bad("invalid sticker set name is specified")
        self.sets[name.lower()] = {
            "name": name, "title": params["title"], "owner": params["user_id"],
            "sticker_type": params.get("sticker_type", "regular"), "stickers": [],
        }
        for item in params["stickers"]:
            self.sets[name.lower()]["stickers"].append(self._store_sticker(name.lower(), item, files))
        return self._ok(True)

    def m_addStickerToSet(self, params, files):
        s = self.sets.get(params["name"].lower())
        if not s:
            return self._bad("STICKERSET_INVALID")
        if params["user_id"] != s["owner"]:
            return self._bad("USER_ID_INVALID")
        s["stickers"].append(self._store_sticker(params["name"].lower(), params["sticker"], files))
        return self._ok(True)

    def _find(self, file_id):
        for s in self.sets.values():
            for i, st in enumerate(s["stickers"]):
                if st["file_id"] == file_id:
                    return s, i
        return None, None

    def m_deleteStickerFromSet(self, params, files):
        s, i = self._find(params["sticker"])
        if s is None:
            return self._bad("STICKER_INVALID")
        s["stickers"].pop(i)
        return self._ok(True)

    def m_setStickerPositionInSet(self, params, files):
        s, i = self._find(params["sticker"])
        if s is None:
            return self._bad("STICKER_INVALID")
        s["stickers"].insert(params["position"], s["stickers"].pop(i))
        return self._ok(True)

    def m_setStickerEmojiList(self, params, files):
        s, i = self._find(params["sticker"])
        if s is None:
            return self._bad("STICKER_INVALID")
        s["stickers"][i]["emoji"] = params["emoji_list"][0]
        return self._ok(True)

    def m_setStickerSetTitle(self, params, files):
        self.sets[params["name"].lower()]["title"] = params["title"]
        return self._ok(True)

    def m_deleteStickerSet(self, params, files):
        self.sets.pop(params["name"].lower(), None)
        return self._ok(True)

    def m_getFile(self, params, files):
        return self._ok({"file_id": params["file_id"], "file_unique_id": "u", "file_path": params["file_id"],
                         "file_size": len(self.files.get(params["file_id"], b""))})


class UpdateMaker:
    """Builds the JSON Telegram would send when a user does something."""

    def __init__(self, user_id: int = 42, first_name: str = "Gary"):
        self.user = {"id": user_id, "is_bot": False, "first_name": first_name}
        self.ids = itertools.count(1)
        self.counter = defaultdict(int)

    def _base_message(self, **extra):
        msg = {"message_id": next(self.ids), "date": int(time.time()),
               "chat": {"id": self.user["id"], "type": "private"}, "from": self.user}
        msg.update(extra)
        return {"update_id": next(self.ids), "message": msg}

    def text(self, text: str):
        extra = {"text": text}
        if text.startswith("/"):
            extra["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return self._base_message(**extra)

    def sticker(self, sticker: dict):
        return self._base_message(sticker=sticker)

    def photo(self, file_id: str, caption: str | None = None):
        extra = {"photo": [{"file_id": file_id, "file_unique_id": file_id + "u", "width": 300, "height": 200}]}
        if caption:
            extra["caption"] = caption
        return self._base_message(**extra)

    def button(self, data: str):
        return {
            "update_id": next(self.ids),
            "callback_query": {
                "id": str(next(self.ids)), "from": self.user, "chat_instance": "x", "data": data,
                "message": {"message_id": 1, "date": int(time.time()),
                            "chat": {"id": self.user["id"], "type": "private"}, "from": BOT_USER, "text": "menu"},
            },
        }
