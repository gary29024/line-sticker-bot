"""Talking to the LINE sticker store.

In plain words: you hand this file a LINE link. It works out which pack the
link points to, asks LINE's servers for the pack's details (name, number of
stickers, animated or not), and downloads the picture files.

LINE keeps every pack on a public file server (a "CDN"). The addresses below
are the same ones the LINE app itself uses:

  Sticker pack details : .../stickershop/v1/product/<id>/iphone/productInfo.meta
  Animated/pop-up pack : .../stickershop/v1/product/<id>/iphone/stickerpack@2x.zip
  Still-picture pack   : .../stickershop/v1/product/<id>/iphone/stickers@2x.zip
  Emoji pack details   : .../sticonshop/v1/<id>/sticon/iphone/meta.json
  Emoji pictures       : .../sticonshop/v1/<id>/sticon/iphone/package(_animation).zip
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from dataclasses import dataclass, field

import httpx

CDN = "https://stickershop.line-scdn.net"
STORE = "https://store.line.me"

# A LINE zip is normally 1-10 MB. Refuse anything absurdly large so a broken
# download can't fill the server's memory.
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024

# Languages to try (in this order) when picking the pack's title.
DEFAULT_LANGS = ("en", "zh-Hant", "ja", "zh-Hans", "ko", "th", "id")


class LineError(Exception):
    """Something went wrong talking to LINE. The message is safe to show users."""


# --------------------------------------------------------------------------
# 1. Understanding links
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LineLink:
    kind: str  # "sticker" or "emoji"
    pack_id: str  # digits for stickers, 24 hex characters for emoji
    lang: str | None = None  # language in the link, e.g. "en" or "zh-Hant"

    @property
    def store_url(self) -> str:
        lang = self.lang or "en"
        shop = "stickershop" if self.kind == "sticker" else "emojishop"
        return f"{STORE}/{shop}/product/{self.pack_id}/{lang}"


_LANG = r"(?:/(?P<lang>[A-Za-z]{2}(?:-[A-Za-z]{2,4})?))?"
_STICKER_PATTERNS = [
    # https://store.line.me/stickershop/product/11537/en
    re.compile(r"line\.me/stickershop/product/(?P<id>\d+)" + _LANG),
    # https://line.me/S/sticker/11537  (the "share" link from the LINE app)
    re.compile(r"line\.me/S/sticker/(?P<id>\d+)" + _LANG),
    # older app links: https://line.me/R/shop/detail/11537
    re.compile(r"line\.me/R/shop/(?:sticker/)?detail/(?P<id>\d+)"),
]
_EMOJI_PATTERNS = [
    # https://store.line.me/emojishop/product/5ad6f0f7031a67c6648c6432/en
    re.compile(r"line\.me/emojishop/product/(?P<id>[0-9a-f]{24})" + _LANG),
    # https://line.me/S/emoji/?id=5ad6f0f7031a67c6648c6432
    re.compile(r"line\.me/S/emoji/?\?(?:[^ ]*&)?id=(?P<id>[0-9a-f]{24})"),
    re.compile(r"line\.me/R/emoji/product/(?P<id>[0-9a-f]{24})"),
]
_LANG_QUERY = re.compile(r"[?&]lang=([A-Za-z]{2}(?:-[A-Za-z]{2,4})?)")


def parse_line_link(text: str) -> LineLink | None:
    """Find a LINE sticker or emoji link anywhere in a message. None if there isn't one."""
    for kind, patterns in (("sticker", _STICKER_PATTERNS), ("emoji", _EMOJI_PATTERNS)):
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                lang = match.groupdict().get("lang")
                if not lang:
                    q = _LANG_QUERY.search(text[match.start() :])
                    lang = q.group(1) if q else None
                return LineLink(kind=kind, pack_id=match.group("id"), lang=lang)
    return None


# --------------------------------------------------------------------------
# 2. Pack details and files
# --------------------------------------------------------------------------


@dataclass
class LineItem:
    """One sticker (or emoji) picture straight from LINE."""

    key: str  # LINE's own id for it, e.g. "52002742" or "001"
    data: bytes  # the PNG / animated PNG file
    animated: bool
    sound: bytes | None = None  # m4a sound for "sound stickers" (only kept for ZIP downloads)


@dataclass
class LinePack:
    link: LineLink
    title: str
    resource_type: str  # what LINE calls it: STATIC, ANIMATION, POPUP, ANIMATION_SOUND ...
    animated: bool
    sticker_ids: list[str]
    items: list[LineItem] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.link.kind

    @property
    def count(self) -> int:
        return len(self.sticker_ids)

    def describe(self) -> str:
        """A short human description, e.g. '40 animated stickers'."""
        what = "emoji" if self.kind == "emoji" else "stickers"
        style = "animated" if self.animated else "still"
        extra = ""
        if "POPUP" in self.resource_type or "EFFECT" in self.resource_type:
            extra = " (pop-up effects)"
        elif "SOUND" in self.resource_type:
            extra = " (sound is dropped - Telegram stickers are silent)"
        elif self.resource_type in ("NAME_TEXT", "PER_STICKER_TEXT", "CUSTOM"):
            extra = " (custom-text pack - the text you type in LINE won't be included)"
        return f"{self.count} {style} {what}{extra}"


def _pick_title(titles: dict, prefer: str | None) -> str:
    for lang in ((prefer,) if prefer else ()) + DEFAULT_LANGS:
        if lang and titles.get(lang):
            return titles[lang]
    for value in titles.values():
        if value:
            return value
    return "LINE stickers"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=15),
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (line-sticker-bot)"},
    )


async def _get(client: httpx.AsyncClient, url: str) -> bytes | None:
    """Download a URL. Returns None if LINE says the file doesn't exist (404/403)."""
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code in (403, 404):
                return None
            resp.raise_for_status()
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise LineError("That LINE pack is unexpectedly large, so I stopped downloading it.")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise LineError(f"Couldn't reach LINE's servers ({exc.__class__.__name__}). Please try again later.") from exc


def _open_zip(data: bytes | None) -> zipfile.ZipFile | None:
    if not data:
        return None
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None


def is_animated_png(data: bytes) -> bool:
    """True if this PNG is really an animated PNG (it has an 'acTL' chunk with >1 frame)."""
    if not data.startswith(b"\x89PNG"):
        return False
    first_image_chunk = data.find(b"IDAT")
    idx = data.find(b"acTL", 0, first_image_chunk if first_image_chunk > 0 else 65536)
    if idx < 0:
        return False
    try:
        frames = int.from_bytes(data[idx + 4 : idx + 8], "big")
    except ValueError:
        return False
    return frames > 1


async def fetch_info(link: LineLink, client: httpx.AsyncClient | None = None) -> LinePack:
    """Look up a pack's title and contents without downloading the pictures."""
    own = client is None
    client = client or _client()
    try:
        if link.kind == "sticker":
            return await _sticker_info(client, link)
        return await _emoji_info(client, link)
    finally:
        if own:
            await client.aclose()


async def download(link: LineLink, client: httpx.AsyncClient | None = None) -> LinePack:
    """Look up a pack and download all of its pictures (in the store's order)."""
    own = client is None
    client = client or _client()
    try:
        pack = await fetch_info(link, client)
        if link.kind == "sticker":
            await _download_stickers(client, pack)
        else:
            await _download_emoji(client, pack)
        if not pack.items:
            raise LineError("I found the pack but couldn't download any pictures from it.")
        return pack
    finally:
        if own:
            await client.aclose()


# ---- stickers -------------------------------------------------------------


async def _sticker_info(client: httpx.AsyncClient, link: LineLink) -> LinePack:
    url = f"{CDN}/stickershop/v1/product/{link.pack_id}/iphone/productInfo.meta"
    raw = await _get(client, url)
    if raw is None:
        raise LineError("LINE says that sticker pack doesn't exist (or it's no longer for sale).")
    try:
        meta = json.loads(raw)
    except ValueError as exc:
        raise LineError("LINE sent back something I couldn't read for that pack.") from exc

    resource_type = (meta.get("stickerResourceType") or "STATIC").upper()
    animated = bool(meta.get("hasAnimation")) or any(
        word in resource_type for word in ("ANIMATION", "POPUP", "EFFECT")
    )
    ids = [str(s["id"]) for s in meta.get("stickers", []) if "id" in s]
    if not ids:
        raise LineError("That sticker pack appears to be empty.")
    return LinePack(
        link=link,
        title=_pick_title(meta.get("title") or {}, link.lang),
        resource_type=resource_type,
        animated=animated,
        sticker_ids=ids,
    )


async def _download_stickers(client: httpx.AsyncClient, pack: LinePack) -> None:
    base = f"{CDN}/stickershop/v1/product/{pack.link.pack_id}/iphone"
    archive = None
    if pack.animated or "SOUND" in pack.resource_type:
        # This bigger zip holds the animations, pop-ups and sounds as well as the still pictures.
        archive = _open_zip(await _get(client, f"{base}/stickerpack@2x.zip"))
    if archive is None:
        archive = _open_zip(await _get(client, f"{base}/stickers@2x.zip"))
    names = set(archive.namelist()) if archive else set()

    def read(name: str) -> bytes | None:
        return archive.read(name) if archive and name in names else None

    for sid in pack.sticker_ids:
        data = None
        if pack.animated:
            # Prefer the moving version: normal animation first, then the pop-up effect.
            data = read(f"animation@2x/{sid}@2x.png") or read(f"popup/{sid}.png")
        data = data or read(f"{sid}@2x.png") or read(f"{sid}.png")
        if data is None:
            # Last resort: fetch this one sticker on its own.
            single = f"{CDN}/stickershop/v1/sticker/{sid}/iPhone"
            if pack.animated:
                data = await _get(client, f"{single}/sticker_animation@2x.png")
            data = data or await _get(client, f"{single}/sticker@2x.png")
        if data is None:
            continue
        pack.items.append(
            LineItem(key=sid, data=data, animated=is_animated_png(data), sound=read(f"sound/{sid}.m4a"))
        )


# ---- emoji ----------------------------------------------------------------

_OG_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_TITLE_SUFFIX = re.compile(r"\s+[–-]\s+[^–|]*\|\s*LINE STORE\s*$")


async def _emoji_title(client: httpx.AsyncClient, link: LineLink) -> str:
    """Emoji packs don't list their name in the data file, so read it from the store web page."""
    try:
        page = await _get(client, link.store_url)
    except LineError:
        page = None
    if page:
        match = _OG_TITLE.search(page.decode("utf-8", "replace"))
        if match:
            title = _TITLE_SUFFIX.sub("", html.unescape(match.group(1))).strip()
            if title:
                return title
    return "LINE emoji"


async def _emoji_info(client: httpx.AsyncClient, link: LineLink) -> LinePack:
    url = f"{CDN}/sticonshop/v1/{link.pack_id}/sticon/iphone/meta.json"
    raw = await _get(client, url)
    if raw is None:
        raise LineError("LINE says that emoji pack doesn't exist (or it's no longer for sale).")
    try:
        meta = json.loads(raw)
    except ValueError as exc:
        raise LineError("LINE sent back something I couldn't read for that emoji pack.") from exc
    resource_type = (meta.get("sticonResourceType") or "STATIC").upper()
    ids = [str(x) for x in meta.get("orders", [])]
    if not ids:
        raise LineError("That emoji pack appears to be empty.")
    return LinePack(
        link=link,
        title=await _emoji_title(client, link),
        resource_type=resource_type,
        animated="ANIMATION" in resource_type,
        sticker_ids=ids,
    )


async def _download_emoji(client: httpx.AsyncClient, pack: LinePack) -> None:
    base = f"{CDN}/sticonshop/v1/{pack.link.pack_id}/sticon/iphone"
    archive = None
    if pack.animated:
        archive = _open_zip(await _get(client, f"{base}/package_animation.zip?v=1"))
    if archive is None:
        archive = _open_zip(await _get(client, f"{base}/package.zip?v=1"))
    names = set(archive.namelist()) if archive else set()

    def read(name: str) -> bytes | None:
        return archive.read(name) if archive and name in names else None

    for key in pack.sticker_ids:
        data = None
        if pack.animated:
            data = read(f"{key}_animation.png")
        data = data or read(f"{key}.png")
        if data is None:
            single = f"{CDN}/sticonshop/v1/sticon/{pack.link.pack_id}/iPhone"
            if pack.animated:
                data = await _get(client, f"{single}/{key}_animation.png")
            data = data or await _get(client, f"{single}/{key}.png")
        if data is None:
            continue
        pack.items.append(LineItem(key=key, data=data, animated=is_animated_png(data)))
