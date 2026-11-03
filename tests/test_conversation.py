"""Whole conversations with the bot, against the pretend Telegram server.

LINE is also faked here (with pictures generated on the fly), so these tests
run offline. tests/test_real_line.py checks the real LINE servers.
"""

import asyncio
import io
import zipfile

import pytest
from telegram import Update

from stickerbot import bot as botmod
from stickerbot import line_store
from stickerbot.config import Config
from stickerbot.storage import SqliteStorage as Storage
from conftest import make_apng, make_png
from fake_telegram import FakeTelegram, UpdateMaker

LINK = "https://store.line.me/stickershop/product/11537/en"


def fake_pack(link: line_store.LineLink) -> line_store.LinePack:
    items = [
        line_store.LineItem("1001", make_apng(), True),
        line_store.LineItem("1002", make_png(), False),
        line_store.LineItem("1003", make_apng((250, 300), frames=6), True),
    ]
    return line_store.LinePack(
        link=link, title="Brown & Cony", resource_type="ANIMATION", animated=True,
        sticker_ids=[i.key for i in items], items=items,
    )


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    async def fake_info(link, client=None):
        pack = fake_pack(link)
        pack.items = []
        return pack

    async def fake_download(link, client=None):
        return fake_pack(link)

    monkeypatch.setattr(line_store, "fetch_info", fake_info)
    monkeypatch.setattr(line_store, "download", fake_download)

    tg = FakeTelegram()
    config = Config(
        bot_token="123:TEST", data_dir=str(tmp_path), admin_ids=frozenset(), allowed_user_ids=frozenset(),
        default_emoji="⭐", max_jobs=2, convert_workers=2, log_level="INFO",
    )
    app = botmod.build_app(config, Storage(config.db_path), request=tg)
    await app.initialize()
    await botmod.post_init(app)
    await app.start()
    yield app, tg
    await app.stop()
    await app.shutdown()


async def send(app, data):
    await app.process_update(Update.de_json(data, app.bot))


async def wait_for(condition, timeout=90):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting")


async def import_pack(app, tg, user):
    await send(app, user.text(LINK))
    assert "Found <b>Brown &amp; Cony</b>" in tg.texts()[-1]
    await send(app, user.button("line:make"))
    assert "What should the Telegram set be called" in tg.texts()[-1]
    await send(app, user.text("My Brown"))
    assert "Which emoji" in tg.texts()[-1]
    await send(app, user.text("🐻 hi ❤️"))
    await wait_for(lambda: any("Done!" in t for t in tg.texts()))


async def test_full_import_flow(setup):
    app, tg = setup
    user = UpdateMaker(user_id=42)

    await send(app, user.text("/start"))
    assert "LINE stickers into Telegram stickers" in tg.texts()[-1]

    tg.flood_once.add("addStickerToSet")  # Telegram says "slow down" once - the bot must wait and retry
    await import_pack(app, tg, user)
    assert not tg.flood_once  # the slow-down really happened

    created = tg.called("createNewStickerSet")
    assert len(created) == 1
    assert created[0]["name"] == "ls11537_by_test_sticker_bot"
    assert created[0]["title"] == "My Brown"
    assert created[0]["user_id"] == 42
    stickers = tg.sets["ls11537_by_test_sticker_bot"]["stickers"]
    assert [s["_format"] for s in stickers] == ["video", "static", "video"]  # order kept, formats right
    assert all(s["emoji"] == "🐻" for s in stickers)
    assert "https://t.me/addstickers/ls11537_by_test_sticker_bot" in tg.texts()[-1]

    # /manage lists the set; opening it shows the menu
    await send(app, user.text("/manage"))
    open_button = tg.last_markup()[0][0]
    assert "My Brown" in open_button["text"]
    await send(app, user.button(open_button["callback_data"]))
    assert "3/120 stickers" in tg.texts()[-1]


async def test_someone_else_gets_existing_set_or_copy(setup):
    app, tg = setup
    await import_pack(app, tg, UpdateMaker(user_id=42))

    other = UpdateMaker(user_id=77)
    await send(app, other.text(LINK))
    await send(app, other.button("line:make"))
    assert "Someone already converted this pack" in tg.texts()[-1]
    await send(app, other.button("imp:copy"))
    await send(app, other.button("imp:title"))
    await send(app, other.button("imp:emoji"))
    await wait_for(lambda: len(tg.called("createNewStickerSet")) == 2)
    await wait_for(lambda: sum("Done!" in t for t in tg.texts()) == 2)
    copy_name = tg.called("createNewStickerSet")[1]["name"]
    assert copy_name.startswith("ls11537_") and copy_name != "ls11537_by_test_sticker_bot"
    assert tg.sets[copy_name.lower()]["stickers"][0]["emoji"] == "⭐"

    # The first user's set can't be edited by the second user
    first = tg.sets["ls11537_by_test_sticker_bot"]["stickers"][0]
    await send(app, other.sticker(_sticker_json(first)))
    assert "Download the whole set" in tg.last_markup()[0][0]["text"]


def _sticker_json(st):
    return {k: v for k, v in st.items() if not k.startswith("_")}


async def test_manage_a_sticker_and_the_set(setup):
    app, tg = setup
    user = UpdateMaker(user_id=42)
    await import_pack(app, tg, user)
    name = "ls11537_by_test_sticker_bot"
    first = tg.sets[name]["stickers"][0]

    # Send a sticker from my set -> options appear
    await send(app, user.sticker(_sticker_json(first)))
    assert "in your set" in tg.texts()[-1]

    # Move it to position 3
    await send(app, user.button("stk:move"))
    assert "currently number 1 of 3" in tg.texts()[-1]
    await send(app, user.text("3"))
    assert tg.sets[name]["stickers"][2]["file_id"] == first["file_id"]

    # Change its emoji
    await send(app, user.sticker(_sticker_json(first)))
    await send(app, user.button("stk:emoji"))
    await send(app, user.text("😎"))
    assert tg.called("setStickerEmojiList")[-1]["emoji_list"] == ["😎"]

    # Delete it
    await send(app, user.sticker(_sticker_json(first)))
    await send(app, user.button("stk:del"))
    await send(app, user.button("stk:del!"))
    assert len(tg.sets[name]["stickers"]) == 2

    # Rename the set
    await send(app, user.button("set:rename"))
    await send(app, user.text("Renamed Bears"))
    assert tg.sets[name]["title"] == "Renamed Bears"

    # Add a photo (converted) and an existing sticker (reused as-is)
    tg.files["PHOTO1"] = make_png((640, 480))
    await send(app, user.button("set:add"))
    await send(app, user.photo("PHOTO1", caption="🎉"))
    assert "Added! The set now has 3" in tg.texts()[-1]
    await send(app, user.sticker(_sticker_json(tg.sets[name]["stickers"][0])))
    assert "Added! The set now has 4" in tg.texts()[-1]
    assert tg.sets[name]["stickers"][2]["emoji"] == "🎉"
    await send(app, user.button("add:done"))

    # Download the set as a ZIP
    await send(app, user.button("set:zip"))
    await wait_for(lambda: tg.sent_documents)
    filename, data = tg.sent_documents[-1]
    assert filename == f"{name}.zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert len([n for n in names if n.count("/") == 1]) == 4

    # Delete the whole set
    await send(app, user.button("set:delete"))
    await send(app, user.button("set:delete!"))
    assert name not in tg.sets
    await send(app, user.text("/manage"))
    assert "don't have any sets yet" in tg.texts()[-1]


async def test_line_zip_download(setup):
    app, tg = setup
    user = UpdateMaker(user_id=5)
    await send(app, user.text("/download"))
    await send(app, user.text(LINK))
    await wait_for(lambda: tg.sent_documents)
    filename, data = tg.sent_documents[-1]
    assert filename == "line_sticker_11537.zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "line_sticker_11537/01_1001.png" in names and "line_sticker_11537/info.txt" in names


async def test_private_bot_blocks_strangers(setup, tmp_path):
    app, tg = setup
    object.__setattr__(app.bot_data["config"], "allowed_user_ids", frozenset({1}))
    await send(app, UpdateMaker(user_id=42).text("/start"))
    assert "private bot" in tg.texts()[-1]
