import asyncio
import itertools
import sys
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberOwner, Message, User
from telethon import errors
from telethon.tl import functions, types

import giftbot.__main__  # noqa: F401 — загружает все модули, чтобы fast_config подменил config везде
from giftbot.config import Config
from giftbot.db import Database

ids = itertools.count(1)
CHANNEL_ID = -1001234567890


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
        self.owners: dict[int, int] = {}
        self.listings: list[tuple[int, int]] = [(3, 9000)]  # (номер, цена) на маркете, новые первыми
        self.floor = 9000
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
                        resell_min_stars=self.floor,
                        availability_resale=len(self.listings),
                    ),
                    types.StarGift(id=2, sticker=types.DocumentEmpty(id=0), stars=15, convert_stars=0),
                ],
            )
        if isinstance(req, functions.payments.GetResaleStarGiftsRequest):
            gifts = [unique("PlushPepe", n, self.issued, price=p).gift for n, p in self.listings[: req.limit]]
            users = unique("PlushPepe", 1, 1).users
            return types.payments.ResaleStarGifts(count=len(gifts), chats=[], users=users, gifts=gifts)
        if isinstance(req, functions.payments.GetStarGiftUpgradeAttributesRequest):
            return types.payments.StarGiftUpgradeAttributes(attributes=unique("PlushPepe", 1, 1).gift.attributes[:3])
        slug, num = req.slug.rsplit("-", 1)
        num = int(num)
        if slug != "PlushPepe" or num > self.issued or num in self.missing:
            raise errors.RPCError(None, "STARGIFT_SLUG_INVALID", 400)
        return unique(slug, num, self.issued, price=self.prices.get(num), owner=self.owners.get(num, 42))


class FakeSession(BaseSession):
    """Сессия Bot API, которая ничего не отправляет, а записывает запросы."""

    def __init__(self):
        super().__init__()
        self.requests = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        name = type(method).__name__
        bot_user = User(id=999, is_bot=True, first_name="Bot", username="test_bot")
        if name == "GetMe":
            return bot_user
        if name == "GetUpdates":
            await asyncio.sleep(0.05)
            return []
        if name == "GetChat":
            from aiogram.types import AcceptedGiftTypes, ChatFullInfo

            gifts = AcceptedGiftTypes(
                unlimited_gifts=False,
                limited_gifts=False,
                unique_gifts=False,
                premium_subscription=False,
                gifts_from_channels=False,
            )
            return ChatFullInfo(
                id=CHANNEL_ID,
                type="channel",
                title="Мой канал",
                accent_color_id=0,
                max_reaction_count=1,
                accepted_gift_types=gifts,
            )
        if name == "GetChatMember":
            if method.user_id == 999:
                # Все обязательные флаги прав — False, кроме тех, что нужны боту в канале.
                rights = {
                    name: name in ("can_post_messages", "can_manage_chat")
                    for name, fld in ChatMemberAdministrator.model_fields.items()
                    if fld.is_required() and fld.annotation is bool
                }
                return ChatMemberAdministrator(user=bot_user, can_post_messages=True, **rights)
            return ChatMemberOwner(user=User(id=method.user_id, is_bot=False, first_name="U"), is_anonymous=False)
        if name in ("SendMessage", "SendPhoto", "SendDocument", "SendInvoice"):
            return Message(
                message_id=next(ids),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None),
                from_user=bot_user,
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
    cfg = Config(requests_per_second=1000, poll_min=0, poll_max=0, market_poll_max=0, admins=frozenset({1}))
    for name, module in list(sys.modules.items()):
        if name.startswith("giftbot") and isinstance(getattr(module, "config", None), Config):
            monkeypatch.setattr(module, "config", cfg)
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
