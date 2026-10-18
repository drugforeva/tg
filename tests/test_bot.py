import itertools
import time
from datetime import datetime

import pytest
from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from conftest import FakeClient

from giftbot.bot import router
from giftbot.delivery import Delivery
from giftbot.models import Filters, Upgrade
from giftbot.watcher import Watcher

ids = itertools.count(1000)
USER = User(id=1, is_bot=False, first_name="U")
BOT_USER = User(id=999, is_bot=True, first_name="Bot")
CHAT = Chat(id=1, type="private")


def upgrade(num, **kw):
    base = dict(
        slug=f"PlushPepe-{num}",
        collection="Plush Pepe",
        gift_id=1,
        num=num,
        issued=num,
        total=5000,
        model="Gold",
        model_rarity=8,
        backdrop="Black",
        backdrop_rarity=20,
        symbol="Star",
        symbol_rarity=5,
        owner_username="ivan",
        detected_at=int(time.time()),
    )
    base.update(kw)
    return Upgrade(**base)


class Harness:
    def __init__(self, bot, db):
        self.bot, self.db = bot, db
        self.delivery = Delivery(bot, db)
        self.watcher = Watcher(FakeClient(), db, self.delivery.on_upgrade)
        self.dp = Dispatcher(db=db, watcher=self.watcher, delivery=self.delivery)
        self.dp.include_router(router)

    async def send(self, text):
        msg = Message(message_id=next(ids), date=datetime.now(), chat=CHAT, from_user=USER, text=text)
        await self.dp.feed_update(self.bot, Update(update_id=next(ids), message=msg))

    async def click(self, data, markup=None):
        msg = Message(message_id=5, date=datetime.now(), chat=CHAT, from_user=BOT_USER, text="x", reply_markup=markup)
        call = CallbackQuery(id=str(next(ids)), from_user=USER, chat_instance="i", data=data, message=msg)
        await self.dp.feed_update(self.bot, Update(update_id=next(ids), callback_query=call))

    def last(self):
        for req in reversed(self.bot.session.requests):
            if type(req).__name__ in ("SendMessage", "EditMessageText"):
                return req
        raise AssertionError("ничего не отправлено")

    def buttons(self, req=None):
        markup = (req or self.last()).reply_markup
        return [b for row in (markup.inline_keyboard if markup else []) for b in row if b.callback_data]


@pytest.fixture
async def h(bot, db, fast_config):
    await db.upsert_collection(1, "Plush Pepe", "PlushPepe", 5000)
    await db.upsert_collection(2, "Durov's Cap", "DurovsCap", 1000)
    await db.save_attributes(
        1,
        [
            ("model", 100, "Gold", 8),
            ("model", 101, "Silver", 30),
            ("backdrop", 1, "Black", 20),
            ("symbol", 200, "Star", 5),
        ],
    )
    for n in range(1, 4):
        await db.save_upgrade(upgrade(n))
    return Harness(bot, db)


async def test_onboarding_and_editing(h):
    await h.send("/start")
    assert "С чего начнём" in h.last().text
    await h.click("pre:rare")
    sub = (await h.db.subscriptions(1))[0]
    assert sub.name == "Редкие модели" and sub.filters.max_model_rarity == 10
    p = f"s:{sub.id}"

    await h.click(f"{p}:ct:i:1:0")  # выбрать коллекцию
    await h.click(f"{p}:at:model:101:0")  # выбрать модель списком
    await h.click(f"{p}:at:backdrop:1:0")
    await h.click(f"{p}:r:symbol:10")
    await h.click(f"{p}:o:1000000")
    await h.click(f"{p}:t:pretty_num")
    await h.click(f"{p}:i:max_price")
    await h.send("5 000")
    await h.click(f"{p}:i:symbols")
    await h.send("star, *moon*")
    await h.click(f"{p}:i:name")
    await h.send("Снайпер")
    f = (await h.db.get_subscription(sub.id)).filters
    assert f.collections == ["Plush Pepe"] and f.models == ["Silver"] and f.backdrops == ["Black"]
    assert f.max_symbol_rarity == 10 and f.min_one_in == 1_000_000 and f.pretty_num
    assert f.max_price == 5000 and f.symbols == ["star", "*moon*"]
    assert (await h.db.get_subscription(sub.id)).name == "Снайпер"
    assert "Снайпер" in h.last().text

    await h.click(f"{p}:at:model:101:0")  # повторное нажатие снимает выбор
    assert (await h.db.get_subscription(sub.id)).filters.models == []

    await h.click(f"{p}:del")
    await h.click(f"{p}:delok")
    assert await h.db.subscriptions(1) == []


async def test_every_button_works(h):
    """Обходит все экраны меню, нажимая каждую кнопку: ничего не падает, callback_data ≤ 64 байт."""
    await h.send("/start")
    await h.click("pre:all")
    queue, seen = [h.last()], set()
    while queue:
        req = queue.pop()
        for button in h.buttons(req):
            data = button.callback_data
            assert len(data.encode()) <= 64, data
            # Удаление и страницы списков проверяем отдельно, чтобы обход был конечным.
            if data in seen or data.endswith(":delok") or len(seen) > 400:
                continue
            seen.add(data)
            before = len(h.bot.session.requests)
            await h.click(data, req.reply_markup)
            if len(h.bot.session.requests) > before:
                queue.append(h.last())
    assert len(seen) > 40


async def test_commands(h):
    await h.send("/start")
    await h.click("pre:all")
    for cmd in ("/menu", "/recent", "/recent 2", "/top", "/stats", "/help", "/pause"):
        await h.send(cmd)
    assert not await h.db.is_active(1)
    await h.send("/resume")
    assert await h.db.is_active(1)
    await h.send("/stats")
    assert "Служебное" in h.last().text  # пользователь 1 — админ


async def test_delivery_card_digest_and_mute(h):
    await h.send("/start")
    await h.click("pre:all")
    await h.db.create_subscription(1, "Вторая", Filters(models=["Gold"]))

    await h.delivery.on_upgrade(upgrade(10, resale_stars=700))
    await h.delivery.flush_all()
    card = h.bot.session.sent()[-1]
    assert "Plush Pepe #10" in card.text and "700 ⭐" in card.text and "Все подряд, Вторая" in card.text
    mute = [b for b in h.buttons(card) if b.callback_data.startswith("mute:")][0]

    for n in range(11, 16):
        await h.delivery.on_upgrade(upgrade(n))
    await h.delivery.flush_all()
    digest = h.bot.session.sent()[-1]
    assert "Новые улучшения: 5" in digest.text

    await h.click(mute.callback_data, card.reply_markup)
    assert all("Plush Pepe" in s.filters.exclude_collections for s in await h.db.subscriptions(1))
    sent = len(h.bot.session.sent())
    await h.delivery.on_upgrade(upgrade(20))
    await h.delivery.flush_all()
    assert len(h.bot.session.sent()) == sent  # скрытая коллекция больше не приходит

    await h.click(f"unmute:{mute.callback_data.split(':')[1]}")
    assert not any(s.filters.exclude_collections for s in await h.db.subscriptions(1))

    await h.send("/pause")
    await h.delivery.on_upgrade(upgrade(21))
    assert not h.delivery.pending


async def test_legacy_filters_migrated(tmp_path):
    import aiosqlite

    from giftbot.db import Database

    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1,"
            " filters TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL)"
        )
        await conn.execute("""INSERT INTO users VALUES (5, 1, '{"models": ["Gold"]}', 0)""")
        await conn.commit()
    db = Database(path)
    await db.connect()
    subs = await db.subscriptions(5)
    assert len(subs) == 1 and subs[0].filters.models == ["Gold"]
    await db.close()
    db = Database(path)
    await db.connect()  # повторная миграция ничего не дублирует
    assert len(await db.subscriptions(5)) == 1
    await db.close()


async def test_command_during_text_input_is_not_swallowed(h):
    await h.send("/start")
    await h.click("pre:all")
    sub = (await h.db.subscriptions(1))[0]
    await h.click(f"s:{sub.id}:i:models")
    await h.send("/recent")
    assert "Последние улучшения" in h.last().text
    assert (await h.db.get_subscription(sub.id)).filters.models == []
    await h.send("Gold")
    assert (await h.db.get_subscription(sub.id)).filters.models == ["Gold"]
