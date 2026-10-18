import dataclasses
import itertools
import json
import time
from datetime import datetime

import pytest
from aiogram import Dispatcher
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineQuery,
    Message,
    MessageOriginChannel,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User,
    WebAppData,
)
from conftest import CHANNEL_ID, FakeClient

from giftbot import handlers
from giftbot.delivery import Delivery
from giftbot.handlers.common import App
from giftbot.market import Market
from giftbot.models import Filters, Upgrade
from giftbot.scheduler import Scheduler
from giftbot.tracker import Tracker
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
        self.client = FakeClient()
        self.delivery = Delivery(bot, db)
        self.market = Market(self.client, db, self.delivery.on_event)
        self.watcher = Watcher(self.client, db, self.delivery.on_event, floor_of=self.market.floor_of)
        self.tracker = Tracker(self.client, db, self.delivery.notify)
        self.app = App(db=db, watcher=self.watcher, delivery=self.delivery, market=self.market, tracker=self.tracker)
        self.dp = Dispatcher(db=db, app=self.app)
        handlers.setup(self.dp)

    async def feed(self, **update):
        await self.dp.feed_update(self.bot, Update(update_id=next(ids), **update))

    def message(self, text=None, **kw):
        return Message(message_id=next(ids), date=datetime.now(), chat=CHAT, from_user=USER, text=text, **kw)

    async def send(self, text):
        await self.feed(message=self.message(text))

    async def click(self, data, markup=None):
        msg = Message(message_id=5, date=datetime.now(), chat=CHAT, from_user=BOT_USER, text="x", reply_markup=markup)
        call = CallbackQuery(id=str(next(ids)), from_user=USER, chat_instance="i", data=data, message=msg)
        await self.feed(callback_query=call)

    def last(self, *names):
        names = names or ("SendMessage", "EditMessageText")
        for req in reversed(self.bot.session.requests):
            if type(req).__name__ in names:
                return req
        raise AssertionError("ничего не отправлено")

    def alerts(self):
        return [r.text for r in self.bot.session.sent("AnswerCallbackQuery") if r.text]

    def buttons(self, req=None):
        markup = (req or self.last()).reply_markup
        rows = getattr(markup, "inline_keyboard", None) or []
        return [b for row in rows for b in row if b.callback_data]


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


@pytest.fixture
def premium(fast_config, monkeypatch):
    """Включает премиум за 100 ⭐; пользователь 1 — не админ, чтобы лимиты действовали."""
    import sys

    cfg = dataclasses.replace(fast_config, premium_price=100, admins=frozenset({777}))
    for name, module in list(sys.modules.items()):
        if name.startswith("giftbot") and getattr(module, "config", None) is fast_config:
            monkeypatch.setattr(module, "config", cfg)
    return cfg


async def test_onboarding_and_editing(h):
    await h.send("/start")
    assert "С чего начнём" in h.last().text
    await h.click("pre:rare")
    sub = (await h.db.subscriptions(1))[0]
    assert sub.name == "Редкие модели" and sub.filters.max_model_rarity == 10
    p = f"s:{sub.id}"

    await h.click(f"{p}:ct:i:1:0")  # выбрать коллекцию
    await h.click(f"{p}:at:model:101:0:i")  # выбрать модель списком
    await h.click(f"{p}:at:backdrop:1:0:i")
    await h.click(f"{p}:at:model:100:0:x")  # исключить модель
    await h.click(f"{p}:r:symbol:10")
    await h.click(f"{p}:o:1000000")
    await h.click(f"{p}:d:20")
    await h.click(f"{p}:ev:listing")
    await h.click(f"{p}:t:pretty_num")
    await h.click(f"{p}:i:max_price")
    await h.send("5 000")
    await h.click(f"{p}:i:symbols")
    await h.send("star, *moon*")
    await h.click(f"{p}:i:numbers")
    await h.send("7, 100-200, мусор")
    await h.click(f"{p}:i:owners")
    await h.send("@ivan, 42")
    await h.click(f"{p}:i:name")
    await h.send("Снайпер")
    sub = await h.db.get_subscription(sub.id)
    f = sub.filters
    assert f.collections == ["Plush Pepe"] and f.models == ["Silver"] and f.backdrops == ["Black"]
    assert f.exclude_models == ["Gold"]
    assert f.max_symbol_rarity == 10 and f.min_one_in == 1_000_000 and f.pretty_num and f.min_discount == 20
    assert f.max_price == 5000 and f.symbols == ["star", "*moon*"] and f.numbers == ["7", "100-200"]
    assert f.owners == ["ivan", "42"] and f.events == ["upgrade", "listing"]
    assert sub.name == "Снайпер" and "Снайпер" in h.last().text

    await h.click(f"{p}:at:model:101:0:i")  # повторное нажатие снимает выбор
    await h.click(f"{p}:ev:upgrade")
    f = (await h.db.get_subscription(sub.id)).filters
    assert f.models == [] and f.events == ["listing"]
    await h.click(f"{p}:ev:listing")  # последнее событие выключить нельзя
    assert (await h.db.get_subscription(sub.id)).filters.events == ["listing"]

    await h.click(f"{p}:del")
    await h.click(f"{p}:delok")
    assert await h.db.subscriptions(1) == []


async def test_every_button_works(h):
    """Обходит все экраны меню, нажимая каждую кнопку: ничего не падает, callback_data ≤ 64 байт."""
    await h.send("/start")
    await h.click("pre:all")
    await h.send("/watch https://t.me/nft/PlushPepe-2")
    await h.send("/menu")
    queue, seen = [h.last()], set()
    while queue:
        req = queue.pop()
        for button in h.buttons(req):
            data = button.callback_data
            assert len(data.encode()) <= 64, data
            # Удаление проверяем отдельно, чтобы обход был конечным.
            if data in seen or data.endswith(":delok") or data.startswith("tw:del") or len(seen) > 600:
                continue
            seen.add(data)
            before = len(h.bot.session.requests)
            await h.click(data, req.reply_markup)
            if len(h.bot.session.requests) > before:
                queue.append(h.last("SendMessage", "EditMessageText", "SendPhoto"))
    assert len(seen) > 70


async def test_commands(h):
    await h.send("/start")
    await h.click("pre:all")
    for cmd in ("/menu", "/recent", "/recent 2", "/top", "/deals", "/stats", "/help", "/daily", "/channels"):
        await h.send(cmd)
    await h.send("/collection pepe")
    assert h.last("SendPhoto").caption.startswith("📈 <b>Plush Pepe</b>")
    await h.send("/collection")
    assert "Коллекции" in h.last().text
    await h.send("/pause")
    assert not await h.db.is_active(1)
    await h.send("/resume")
    assert await h.db.is_active(1)
    await h.send("/stats")
    assert "Служебное" in h.last().text  # пользователь 1 — админ
    await h.send("/premium")
    assert "бесплатно" in h.last().text


async def test_delivery_card_digest_and_mute(h):
    await h.send("/start")
    await h.click("pre:all")
    await h.db.create_subscription(1, "Вторая", Filters(models=["Gold"]))

    await h.delivery.on_event(upgrade(10, resale_stars=700, floor_stars=1000))
    await h.delivery.flush_all()
    card = h.bot.session.sent()[-1]
    assert "Plush Pepe #10" in card.text and "700 ⭐" in card.text and "Все подряд, Вторая" in card.text
    assert "−30% от флора" in card.text
    callbacks = [b.callback_data for b in h.buttons(card)]
    assert "tw:add:PlushPepe-10" in callbacks and "wo:ivan" in callbacks
    mute = next(c for c in callbacks if c.startswith("mute:"))

    for n in range(11, 16):
        await h.delivery.on_event(upgrade(n))
    await h.delivery.flush_all()
    assert "Новые улучшения: 5" in h.bot.session.sent()[-1].text

    await h.click(mute, card.reply_markup)
    assert all("Plush Pepe" in s.filters.exclude_collections for s in await h.db.subscriptions(1))
    sent = len(h.bot.session.sent())
    await h.delivery.on_event(upgrade(20))
    await h.delivery.flush_all()
    assert len(h.bot.session.sent()) == sent  # скрытая коллекция больше не приходит

    await h.click(f"unmute:{mute.split(':')[1]}")
    assert not any(s.filters.exclude_collections for s in await h.db.subscriptions(1))

    await h.send("/pause")
    await h.delivery.on_event(upgrade(21))
    assert not h.delivery.pending


async def test_listing_events_and_market(h):
    await h.send("/start")
    await h.click("pre:snipe")
    sub = (await h.db.subscriptions(1))[0]
    assert sub.filters.events == ["listing"] and sub.filters.min_discount == 15

    await h.market.refresh_catalog()
    assert h.market.floor_of(1) == 9000
    assert await h.market.poll(1) == 0  # первый взгляд — молча

    h.client.listings = [(7, 6000), (8, 8800), (3, 9000)]  # два новых лота, один на 33% дешевле флора
    h.client.floor = 6000
    await h.market.refresh_catalog()
    assert h.market.due[1] == 0
    assert await h.market.poll(1) == 2
    await h.delivery.flush_all()
    card = h.bot.session.sent()[-1]
    assert "PlushPepe-7" in card.text and "выставлен на продажу" in card.text and "−33% от флора" in card.text
    assert [b.text for b in card.reply_markup.inline_keyboard[0]][0] == "🛒 Купить"
    assert await h.market.poll(1) == 0  # повторно те же лоты не приходят

    await h.send("/deals")
    assert "PlushPepe-7" in h.last().text


async def test_channel_target(h):
    await h.send("/start")
    await h.click("pre:all")
    sub = (await h.db.subscriptions(1))[0]
    await h.send(f"/connect {CHANNEL_ID}")
    assert "подключён" in h.last().text
    await h.click(f"s:{sub.id}:dst")
    assert any(b.callback_data == f"s:{sub.id}:dst:{CHANNEL_ID}" for b in h.buttons())
    await h.click(f"s:{sub.id}:dst:{CHANNEL_ID}")
    assert (await h.db.get_subscription(sub.id)).target_id == CHANNEL_ID

    await h.delivery.on_event(upgrade(30))
    await h.delivery.flush_all()
    post = h.bot.session.sent()[-1]
    assert post.chat_id == CHANNEL_ID
    assert not any(b.callback_data for row in post.reply_markup.inline_keyboard for b in row)  # без служебных кнопок

    await h.click(f"ch:del:{CHANNEL_ID}")
    assert (await h.db.get_subscription(sub.id)).target_id is None

    forwarded = h.message(
        "пост",
        forward_origin=MessageOriginChannel(
            type="channel", date=datetime.now(), chat=Chat(id=CHANNEL_ID, type="channel"), message_id=1
        ),
    )
    await h.feed(message=forwarded)
    assert await h.db.channel_ids() == {CHANNEL_ID}


async def test_watch_gift_and_people(h):
    await h.send("/start")
    await h.click("pre:all")
    await h.send("/watch https://t.me/nft/PlushPepe-5")
    assert await h.db.tracked_of(1) == ["PlushPepe-5"]

    assert await h.tracker.check("PlushPepe-5", None) == []  # первый снимок
    h.client.prices[5] = 4000
    changes = await h.tracker.check("PlushPepe-5", (await h.db.tracked_due(int(time.time()) + 1, 10))[0][1])
    assert [c[0] for c in changes] == ["listed"]
    assert "Выставлен на продажу" in h.bot.session.sent()[-1].text
    h.client.prices.pop(5)
    h.client.owners[5] = 7
    state = (await h.db.tracked_due(int(time.time()) + 1, 10))[0][1]
    assert [c[0] for c in await h.tracker.check("PlushPepe-5", state)] == ["sold"]
    assert "Продан за 4 000 ⭐" in h.bot.session.sent()[-1].text

    await h.click("tw:del:PlushPepe-5")
    assert await h.db.tracked_of(1) == []

    await h.click("wo:ivan")
    people = [s for s in await h.db.subscriptions(1) if s.name == "👤 Люди"]
    assert people and people[0].filters.owners == ["ivan"]

    await h.send("смотри https://t.me/nft/PlushPepe-4")  # карточка по ссылке
    assert "Plush Pepe #4" in h.last().text and "Владелец: @ivan" in h.last().text


async def test_inline_and_webapp(h):
    await h.feed(inline_query=InlineQuery(id="q1", from_user=USER, query="gold", offset=""))
    answer = h.last("AnswerInlineQuery")
    assert answer.results and answer.results[0].title.startswith("Plush Pepe #")

    await h.send("/start")
    await h.click("pre:all")
    sub = (await h.db.subscriptions(1))[0]
    payload = {
        "id": sub.id,
        "name": "Из редактора",
        "filters": {"collections": ["Plush Pepe"], "max_num": "500", "pretty_num": 1, "events": ["listing", "hack"]},
    }
    await h.feed(message=h.message(web_app_data=WebAppData(data=json.dumps(payload), button_text="x")))
    sub = await h.db.get_subscription(sub.id)
    assert sub.name == "Из редактора" and sub.filters.collections == ["Plush Pepe"]
    assert sub.filters.max_num == 500 and sub.filters.pretty_num and sub.filters.events == ["listing"]

    stranger = {"id": sub.id + 999, "filters": {}}
    await h.feed(message=h.message(web_app_data=WebAppData(data=json.dumps(stranger), button_text="x")))
    assert "не найдена" in h.last().text


async def test_premium_limits_and_payment(h, premium):
    await h.send("/start")
    await h.click("pre:all")
    await h.click("pre:pretty")
    await h.click("pre:sale")  # третья — сверх бесплатного лимита
    assert len(await h.db.subscriptions(1)) == 2 and any("Больше подписок" in a for a in h.alerts())
    sub = (await h.db.subscriptions(1))[0]
    await h.click(f"s:{sub.id}:ev:listing")
    assert (await h.db.get_subscription(sub.id)).filters.events == ["upgrade"]

    await h.send("/premium")
    invoice = h.last("SendInvoice")
    assert invoice.currency == "XTR" and invoice.prices[0].amount == 100
    query = PreCheckoutQuery(id="p", from_user=USER, currency="XTR", total_amount=100, invoice_payload=invoice.payload)
    await h.feed(pre_checkout_query=query)
    assert h.last("AnswerPreCheckoutQuery").ok
    paid = SuccessfulPayment(
        currency="XTR",
        total_amount=100,
        invoice_payload=invoice.payload,
        telegram_payment_charge_id="charge-1",
        provider_payment_charge_id="",
    )
    await h.feed(message=h.message(successful_payment=paid))
    assert await h.db.premium_until(1) > time.time() + 29 * 86400
    await h.click(f"s:{sub.id}:ev:listing")
    assert "listing" in (await h.db.get_subscription(sub.id)).filters.events


async def test_scheduler_daily_and_backup(h, fast_config, monkeypatch):
    from zoneinfo import ZoneInfo

    await h.send("/start")
    await h.click("pre:all")
    await h.click("daily:10")
    assert await h.db.get_daily(1) == 10
    cfg = dataclasses.replace(fast_config, backup_hour=10)
    monkeypatch.setattr("giftbot.scheduler.config", cfg)
    scheduler = Scheduler(h.db, h.delivery)
    now = datetime(2026, 10, 19, 10, 5, tzinfo=ZoneInfo("Europe/Moscow"))
    await scheduler.tick(now)
    assert "Сводка за сутки" in h.bot.session.sent()[-1].text
    assert h.bot.session.sent("SendDocument")  # бэкап админу
    sent = len(h.bot.session.requests)
    await scheduler.tick(now)  # второй раз в тот же день — ничего
    assert len(h.bot.session.requests) == sent


async def test_admin_commands(h):
    await h.send("/backup")
    assert h.bot.session.sent("SendDocument")
    await h.send("/start")
    await h.send("/broadcast Привет всем")
    assert "Разослано: 1 из 1" in h.last().text


async def test_command_during_text_input_is_not_swallowed(h):
    await h.send("/start")
    await h.click("pre:all")
    sub = (await h.db.subscriptions(1))[0]
    await h.click(f"s:{sub.id}:i:models")
    await h.send("/recent")
    assert "Последнее по вашим подпискам" in h.last().text
    assert (await h.db.get_subscription(sub.id)).filters.models == []
    await h.send("Gold")
    assert (await h.db.get_subscription(sub.id)).filters.models == ["Gold"]


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
    await db.delete_subscription(subs[0].id)
    await db.close()
    db = Database(path)
    await db.connect()  # повторная миграция не воскрешает удалённую подписку
    assert await db.subscriptions(5) == []
    await db.close()
