import asyncio
import itertools
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, User
from telethon import errors
from telethon.tl import functions, types

from giftbot import watcher as watcher_module
from giftbot.config import Config
from giftbot.db import Database

ids = itertools.count(1)


def unique(slug, num, issued, model="Gold", backdrop="Black", owner=42, sender=7, price=None):
    return types.payments.UniqueStarGift(
        gift=types.StarGiftUnique(
            id=num,
            gift_id=1,
            title="Plush Pepe",
            slug=f"{slug}-{num}",
            num=num,
            attributes=[
                types.StarGiftAttributeModel(
                    name=model, document=types.DocumentEmpty(id=100), rarity=types.StarGiftAttributeRarity(permille=8)
                ),
                types.StarGiftAttributeBackdrop(
                    name=backdrop,
                    backdrop_id=1,
                    center_color=0,
                    edge_color=0,
                    pattern_color=0,
                    text_color=0,
                    rarity=types.StarGiftAttributeRarity(permille=20),
                ),
                types.StarGiftAttributePattern(
                    name="Star", document=types.DocumentEmpty(id=200), rarity=types.StarGiftAttributeRarityLegendary()
                ),
                types.StarGiftAttributeOriginalDetails(
                    recipient_id=types.PeerUser(owner), date=None, sender_id=types.PeerUser(sender)
                ),
            ],
            availability_issued=issued,
            availability_total=5000,
            owner_id=types.PeerUser(owner),
            resell_amount=[types.StarsAmount(amount=price, nanos=0)] if price else None,
            value_usd_amount=4520,
        ),
        chats=[],
        users=[types.User(id=owner, first_name="Ivan", username="ivan"), types.User(id=sender, first_name="Petya")],
    )


class FakeClient:
    """Имитация Telethon: одна коллекция Plush Pepe, слаг на маркете — PlushPepe."""

    def __init__(self, title="Plush Pepe"):
        self.title = title
        self.issued = 10
        self.missing: set[int] = set()
        self.prices: dict[int, int] = {}
        self.calls = []

    async def __call__(self, req):
        self.calls.append(type(req).__name__)
        if isinstance(req, functions.payments.GetStarGiftsRequest):
            return types.payments.StarGifts(
                hash=0,
                chats=[],
                users=[],
                gifts=[
                    types.StarGift(
                        id=1,
                        sticker=types.DocumentEmpty(id=0),
                        stars=350,
                        convert_stars=0,
                        title=self.title,
                        upgrade_stars=100,
                        availability_total=5000,
                    ),
                    types.StarGift(id=2, sticker=types.DocumentEmpty(id=0), stars=15, convert_stars=0),
                ],
            )
        if isinstance(req, functions.payments.GetResaleStarGiftsRequest):
            return types.payments.ResaleStarGifts(
                count=1, chats=[], users=[], gifts=[unique("PlushPepe", 3, self.issued).gift]
            )
        if isinstance(req, functions.payments.GetStarGiftUpgradeAttributesRequest):
            return types.payments.StarGiftUpgradeAttributes(attributes=unique("PlushPepe", 1, 1).gift.attributes[:3])
        slug, num = req.slug.rsplit("-", 1)
        num = int(num)
        if slug != "PlushPepe" or num > self.issued or num in self.missing:
            raise errors.RPCError(None, "STARGIFT_SLUG_INVALID", 400)
        return unique(slug, num, self.issued, price=self.prices.get(num))


class FakeSession(BaseSession):
    """Сессия Bot API, которая ничего не отправляет, а записывает запросы."""

    def __init__(self):
        super().__init__()
        self.requests = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return User(id=999, is_bot=True, first_name="Bot", username="test_bot")
        if name == "GetUpdates":
            await asyncio.sleep(0.05)
            return []
        if name == "SendMessage":
            return Message(
                message_id=next(ids),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
                from_user=User(id=999, is_bot=True, first_name="Bot"),
            )
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    def sent(self, name="SendMessage"):
        return [r for r in self.requests if type(r).__name__ == name]


@pytest.fixture
def fast_config(monkeypatch):
    cfg = Config(requests_per_second=1000, poll_min=0, poll_max=0, admins=frozenset({1}))
    for module in ("giftbot.watcher", "giftbot.bot", "giftbot.delivery", "giftbot.config"):
        monkeypatch.setattr(f"{module}.config", cfg, raising=False)
    watcher_module.config = cfg
    return cfg


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def bot():
    return Bot("42:TEST", session=FakeSession())


@pytest.fixture(autouse=True)
def detach_router():
    """Роутер бота модульный, а диспетчер в каждом тесте новый."""
    from giftbot.bot import router

    router._parent_router = None
    yield
    router._parent_router = None
