"""Checks against the real LINE servers. Skipped unless RUN_NETWORK_TESTS=1."""

import os

import pytest

from stickerbot import convert, line_store

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("RUN_NETWORK_TESTS") != "1", reason="set RUN_NETWORK_TESTS=1"),
]


@pytest.mark.parametrize(
    "url,kind,animated",
    [
        ("https://store.line.me/stickershop/product/12345/en", "sticker", False),
        ("https://line.me/S/sticker/11537", "sticker", True),
        ("https://store.line.me/stickershop/product/8000/en", "sticker", True),  # pop-up + sound
        ("https://store.line.me/emojishop/product/5ad6f0f7031a67c6648c6432/en", "emoji", False),
        ("https://store.line.me/emojishop/product/612762fee63ed0172f7c3660/en", "emoji", True),
    ],
)
async def test_real_pack_downloads_and_first_item_converts(url, kind, animated):
    pack = await line_store.download(line_store.parse_line_link(url))
    assert pack.kind == kind and pack.animated == animated
    assert len(pack.items) == pack.count > 0
    assert pack.title and pack.title != "LINE emoji"
    result = await convert.convert(pack.items[0].data, emoji=kind == "emoji")
    assert result.format == ("video" if animated else "static")


async def test_missing_pack_gives_friendly_error():
    with pytest.raises(line_store.LineError):
        await line_store.fetch_info(line_store.LineLink("sticker", "999999999"))


async def test_real_pack_title_follows_language():
    link = line_store.parse_line_link("https://line.me/S/sticker/11537")
    en = await line_store.fetch_info(link, lang="en")
    zh = await line_store.fetch_info(link, lang="zh")
    assert en.title.startswith("Brown")
    assert zh.title != en.title and any("\u4e00" <= ch <= "\u9fff" for ch in zh.title)
    emoji = line_store.parse_line_link("https://store.line.me/emojishop/product/612762fee63ed0172f7c3660/en")
    assert any("\u4e00" <= ch <= "\u9fff" for ch in (await line_store.fetch_info(emoji, lang="zh")).title)
