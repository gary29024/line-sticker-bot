"""Small checks of the building blocks: links, names, emoji, conversion, storage."""

import asyncio
import io
import json
import os
import subprocess
import tempfile

import pytest
from PIL import Image

from stickerbot import convert, line_store, telegram_sets
from stickerbot.bot import parse_emojis
from stickerbot.storage import Storage
from conftest import make_apng, make_gif, make_png


# ---- LINE links -------------------------------------------------------------

@pytest.mark.parametrize(
    "text,kind,pack_id,lang",
    [
        ("https://store.line.me/stickershop/product/11537/en", "sticker", "11537", "en"),
        ("look https://store.line.me/stickershop/product/11537/zh-Hant cute!", "sticker", "11537", "zh-Hant"),
        ("https://store.line.me/stickershop/product/8525", "sticker", "8525", None),
        ("https://line.me/S/sticker/11537", "sticker", "11537", None),
        ("https://line.me/S/sticker/11537/?lang=ja&ref=lsh_stickerDetail", "sticker", "11537", "ja"),
        ("https://line.me/R/shop/detail/1351", "sticker", "1351", None),
        ("https://store.line.me/emojishop/product/5ad6f0f7031a67c6648c6432/en", "emoji", "5ad6f0f7031a67c6648c6432", "en"),
        ("https://line.me/S/emoji/?id=612762fee63ed0172f7c3660&lang=zh-Hant", "emoji", "612762fee63ed0172f7c3660", "zh-Hant"),
        ("https://line.me/S/emoji?id=612762fee63ed0172f7c3660", "emoji", "612762fee63ed0172f7c3660", None),
    ],
)
def test_parse_line_link(text, kind, pack_id, lang):
    link = line_store.parse_line_link(text)
    assert link == line_store.LineLink(kind, pack_id, lang)


@pytest.mark.parametrize("text", ["hello", "https://t.me/addstickers/foo", "https://store.line.me/themeshop/product/x"])
def test_parse_line_link_rejects(text):
    assert line_store.parse_line_link(text) is None


def test_animated_png_detection():
    assert line_store.is_animated_png(make_apng())
    assert not line_store.is_animated_png(make_png())
    assert not line_store.is_animated_png(b"GIF89a....")


# ---- Telegram names ---------------------------------------------------------

NAME_RULE = r"^[A-Za-z][A-Za-z0-9_]*$"


@pytest.mark.parametrize("bot", ["mybot", "a_very_long_sticker_bot_name_xyz"])
@pytest.mark.parametrize("pack_id", ["11537", "612762fee63ed0172f7c3660"])
@pytest.mark.parametrize("copy", [False, True])
def test_set_names_follow_telegram_rules(bot, pack_id, copy):
    import re

    name = telegram_sets.make_set_name("le", pack_id, bot, copy=copy)
    assert len(name) <= 64
    assert name.endswith(f"_by_{bot}")
    assert re.match(NAME_RULE, name)
    assert "__" not in name


def test_copy_names_differ():
    a = telegram_sets.make_set_name("ls", "11537", "mybot", copy=True)
    b = telegram_sets.make_set_name("ls", "11537", "mybot", copy=True)
    assert a != b
    assert telegram_sets.make_set_name("ls", "11537", "mybot") == "ls11537_by_mybot"


def test_parse_telegram_set_link():
    assert telegram_sets.parse_telegram_set_link("https://t.me/addstickers/Cute_by_bot") == "Cute_by_bot"
    assert telegram_sets.parse_telegram_set_link("t.me/addemoji/Emo") == "Emo"
    assert telegram_sets.parse_telegram_set_link("https://store.line.me/x") is None


def test_parse_emojis():
    assert parse_emojis("😀") == ["😀"]
    assert parse_emojis("love ❤️ and 👍🏽 and 😀😀") == ["❤️", "👍🏽", "😀"]
    assert parse_emojis("no emoji here") == []


# ---- Conversion -------------------------------------------------------------

def probe(webm: bytes) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(webm)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_name,width,height,r_frame_rate:stream_tags=alpha_mode:format=duration",
             "-of", "json", f.name],
            capture_output=True, text=True, check=True,
        ).stdout
    finally:
        os.unlink(f.name)
    data = json.loads(out)
    stream = data["streams"][0]
    num, den = map(int, stream["r_frame_rate"].split("/"))
    return {
        "codec": stream["codec_name"], "w": stream["width"], "h": stream["height"], "fps": num / den,
        "duration": float(data["format"]["duration"]), "alpha": stream.get("tags", {}).get("alpha_mode"),
    }


def test_static_sticker_is_512():
    result = convert.to_static(make_png((370, 320)))
    image = Image.open(io.BytesIO(result.data))
    assert result.format == "static"
    assert max(image.size) == 512 and image.size[1] <= 512
    assert len(result.data) <= convert.MAX_STATIC_BYTES


def test_tall_static_sticker():
    image = Image.open(io.BytesIO(convert.to_static(make_png((200, 400))).data))
    assert image.size[1] == 512 and image.size[0] <= 512


def test_static_emoji_is_100_square():
    image = Image.open(io.BytesIO(convert.to_static(make_png((180, 150)), emoji=True).data))
    assert image.size == (100, 100)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0  # see-through padding


async def test_animated_png_becomes_valid_video_sticker():
    result = await convert.convert(make_apng())
    assert result.format == "video" and result.extension == "webm"
    info = probe(result.data)
    assert info["codec"] == "vp9"
    assert max(info["w"], info["h"]) == 512
    assert info["duration"] <= 3.0
    assert info["fps"] <= 30
    assert info["alpha"] == "1"  # transparency kept
    assert len(result.data) <= convert.MAX_VIDEO_BYTES


async def test_long_animation_is_sped_up():
    result = await convert.convert(make_apng(frames=40, duration=125))  # 5 seconds long
    assert probe(result.data)["duration"] <= 3.0


async def test_animated_emoji_is_100_square():
    info = probe((await convert.convert(make_apng((180, 180)), emoji=True)).data)
    assert (info["w"], info["h"]) == (100, 100)


async def test_gif_and_video_inputs():
    gif = await convert.convert(make_gif())
    assert gif.format == "video" and max(probe(gif.data)["w"], probe(gif.data)["h"]) == 512

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "clip.mp4")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=8",
             "-pix_fmt", "yuv420p", path],
            check=True,
        )
        with open(path, "rb") as f:
            mp4 = await convert.convert(f.read())
    info = probe(mp4.data)
    assert mp4.format == "video" and info["w"] == 512 and info["duration"] <= 3.0
    assert len(mp4.data) <= convert.MAX_VIDEO_BYTES


async def test_tgs_is_refused():
    with pytest.raises(convert.ConvertError):
        await convert.convert(b"\x1f\x8b\x08\x00rest-of-gzip")


def test_timeline_respects_limits():
    fps, order = convert.plan_timeline([100.0] * 10, 30)  # 1 s at 10 fps
    assert fps == 10 and order == list(range(10))
    fps, order = convert.plan_timeline([50.0] * 100, 30)  # 5 s -> sped up
    assert len(order) / fps <= convert.MAX_SECONDS + 1e-9
    assert order == sorted(order) and order[-1] >= 90  # reaches the end of the animation


# ---- Storage ----------------------------------------------------------------

def test_storage_roundtrip(tmp_path):
    db = Storage(str(tmp_path / "x.sqlite3"))
    rec = db.add_set("ls1_by_bot", "Title", 42, "regular", "https://line")
    assert db.get_by_name("LS1_BY_BOT").id == rec.id
    assert [r.name for r in db.list_for_owner(42)] == ["ls1_by_bot"]
    db.rename("ls1_by_bot", "New")
    assert db.get_by_id(rec.id).title == "New"
    assert rec.link == "https://t.me/addstickers/ls1_by_bot"
    db.delete("ls1_by_bot")
    assert db.list_for_owner(42) == []


# ---- Polite retries ---------------------------------------------------------

async def test_add_sticker_does_not_duplicate_after_timeout():
    from telegram.error import TimedOut

    class Bot:
        def __init__(self):
            self.count = 1
            self.calls = 0

        async def add_sticker_to_set(self, **kwargs):
            self.calls += 1
            self.count += 1  # the sticker arrives...
            raise TimedOut()  # ...but our connection times out before Telegram answers

        async def get_sticker_set(self, name):
            class S:
                stickers = [None] * self.count
            return S()

    bot = Bot()
    await telegram_sets.add_sticker(bot, 1, "x", object(), count_before=1)
    assert bot.calls == 1 and bot.count == 2


def test_long_gif_keeps_memory_small():
    from PIL import Image as PILImage

    frames = [PILImage.new("RGB", (64, 64), (i % 255, 0, 0)) for i in range(200)]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=20, loop=0)  # 4 s
    kept, durations = convert._image_frames(buf.getvalue(), emoji=False)
    assert len(kept) <= 90  # not 200
    assert abs(sum(durations) - 4000) < 1  # timing preserved (gets sped up later)

    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:] * 2, duration=40, loop=0)  # 16 s
    kept, durations = convert._image_frames(buf.getvalue(), emoji=False)
    assert sum(durations) <= convert.MAX_SECONDS * 1000 + 1  # cut, not sped up
