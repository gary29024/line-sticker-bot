"""The bot speaks English and Traditional Chinese (Hong Kong)."""

import string

import pytest

from stickerbot import convert, i18n, line_store
from stickerbot import bot as botmod
from stickerbot.config import Config
from stickerbot.storage import SqliteStorage
from fake_telegram import FakeTelegram, UpdateMaker
from test_conversation import LINK, send, setup, wait_for  # noqa: F401  (setup is a fixture)


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


@pytest.mark.parametrize("key", sorted(i18n.TEXTS))
def test_every_message_has_both_languages(key):
    entry = i18n.TEXTS[key]
    assert entry.get("en") and entry.get("zh"), f"{key} is missing a language"
    # Both versions must fill in the same blanks, or one of them would crash
    assert _fields(entry["en"]) == _fields(entry["zh"]), key


@pytest.mark.parametrize(
    "code,lang", [("zh-hant", "zh"), ("zh-hk", "zh"), ("zh-TW", "zh"), ("zh-hans", "zh"), ("zh", "zh"),
                  ("en", "en"), ("ja", "en"), (None, "en"), ("", "en")],
)
def test_language_from_telegram_app(code, lang):
    assert i18n.lang_from_telegram(code) == lang


def test_errors_speak_both_languages():
    err = line_store.LineError("line.unreachable", error="ConnectTimeout")
    assert err.text("en") == "Couldn't reach LINE's servers (ConnectTimeout). Please try again later."
    assert err.text("zh") == "無法連接 LINE 的伺服器（ConnectTimeout），請稍後再試。"
    assert str(err) == err.text("en")  # logs stay in English
    assert convert.ConvertError("conv.tgs").text("zh").startswith("無法轉換")


def test_pack_description_in_both_languages():
    pack = line_store.LinePack(
        link=line_store.LineLink("sticker", "1"), title="x", resource_type="ANIMATION_SOUND", animated=True,
        sticker_ids=["1"] * 24,
    )
    assert pack.describe("en") == "24 animated stickers (sound is dropped – Telegram stickers are silent)"
    assert pack.describe("zh") == "24 個動態貼圖（聲音會被移除，Telegram 貼圖沒有聲音）"


def test_title_language_order():
    assert line_store.title_languages("zh", "en")[0] == "zh-Hant"
    assert line_store.title_languages("en", "ja")[:2] == ("en", "ja")
    titles = {"en": "Brown", "ja": "ブラウン", "zh-Hant": "熊大"}
    assert line_store._pick_title(titles, line_store.title_languages("zh", None)) == "熊大"
    assert line_store._pick_title(titles, line_store.title_languages("en", "zh-Hant")) == "Brown"
    assert line_store._pick_title({"ja": "ブラウン"}, line_store.title_languages("zh", None)) == "ブラウン"


async def test_chinese_user_gets_chinese_everything(setup):
    app, tg = setup
    user = UpdateMaker(user_id=88, language_code="zh-hant")

    await send(app, user.text("/start"))
    assert "你好！我可以把 LINE 貼圖變成 Telegram 貼圖" in tg.texts()[-1]
    assert tg.last_markup()[0][0]["text"] == "📥 匯入 LINE 貼圖"

    await send(app, user.text(LINK))
    assert "找到 <b>熊大與兔兔</b>" in tg.texts()[-1]  # the pack's Chinese name
    assert "個動態貼圖" in tg.texts()[-1]
    await send(app, user.button("line:make"))
    assert "要叫甚麼名字" in tg.texts()[-1]
    await send(app, user.button("imp:title"))
    assert "配對哪個表情符號" in tg.texts()[-1]
    await send(app, user.button("imp:emoji"))
    await wait_for(lambda: any("完成！" in t for t in tg.texts()), tg=tg)

    created = tg.called("createNewStickerSet")[-1]
    assert created["title"] == "熊大與兔兔"
    done = next(t for t in tg.texts() if "完成！" in t)
    assert "已加入 3 張貼圖" in done
    assert any("正在從 LINE 下載" in t for t in tg.texts())  # progress messages in Chinese

    await send(app, user.text("hello?"))
    assert "我不太明白" in tg.texts()[-1]


async def test_command_menu_in_both_languages(setup):
    _, tg = setup
    menus = {p.get("language_code"): p["commands"] for p in tg.called("setMyCommands")}
    assert [c["command"] for c in menus[None]] == list(botmod.COMMANDS)
    assert menus[None][0]["description"] == "Welcome & help"
    assert menus["zh"][0]["description"] == "開始及使用說明"


async def test_language_choice_is_remembered_after_restart(tmp_path):
    """Pick English while your Telegram app is in Chinese; the bot remembers it even after
    it restarts (on Cloudflare it restarts every time it wakes up)."""
    config = Config(
        bot_token="123:TEST", data_dir=str(tmp_path), admin_ids=frozenset(), allowed_user_ids=frozenset(),
        default_emoji="⭐", max_jobs=2, convert_workers=2, log_level="INFO",
    )
    user = UpdateMaker(user_id=7, language_code="zh-hk")

    async def run(steps):
        tg = FakeTelegram()
        app = botmod.build_app(config, SqliteStorage(config.db_path), request=tg)
        await app.initialize()
        await botmod.post_init(app)
        try:
            for step in steps:
                await send(app, step)
        finally:
            await app.shutdown()
        return tg

    tg = await run([user.text("/start")])
    assert "你好" in tg.texts()[-1]

    tg = await run([user.text("/language"), user.button("lang:en")])
    assert "I'll speak English" in tg.texts()[-2]

    tg = await run([user.text("/start")])  # a fresh bot, same database
    assert "Hi! I turn LINE stickers" in tg.texts()[-1]

    tg = await run([user.text("/language"), user.button("lang:auto")])
    assert "跟隨你的 Telegram app 語言" in tg.texts()[-2]
    tg = await run([user.text("/start")])
    assert "你好" in tg.texts()[-1]
