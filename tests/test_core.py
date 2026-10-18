import asyncio

from telethon.tl import functions, types

from giftbot.bot import format_upgrade
from giftbot.db import Database
from giftbot.filters import is_pretty_number, matches
from giftbot.models import Filters
from giftbot.watcher import Watcher, title_to_slug


def unique(slug, num, issued, model="Gold", backdrop="Black", owner=42):
    rarity = types.StarGiftAttributeRarity(permille=8)
    return types.payments.UniqueStarGift(
        gift=types.StarGiftUnique(
            id=num, gift_id=1, title="Plush Pepe", slug=f"{slug}-{num}", num=num,
            attributes=[
                types.StarGiftAttributeModel(name=model, document=types.DocumentEmpty(id=0), rarity=rarity),
                types.StarGiftAttributeBackdrop(name=backdrop, backdrop_id=1, center_color=0, edge_color=0,
                                                pattern_color=0, text_color=0, rarity=types.StarGiftAttributeRarity(20)),
                types.StarGiftAttributePattern(name="Star", document=types.DocumentEmpty(id=0),
                                               rarity=types.StarGiftAttributeRarityLegendary()),
            ],
            availability_issued=issued, availability_total=5000,
            owner_id=types.PeerUser(owner),
            resell_amount=[types.StarsAmount(amount=999, nanos=0)] if num % 2 else None,
        ),
        chats=[],
        users=[types.User(id=owner, first_name="Ivan", username="ivan")],
    )


class FakeClient:
    def __init__(self):
        self.issued = 10

    async def __call__(self, req):
        if isinstance(req, functions.payments.GetStarGiftsRequest):
            return types.payments.StarGifts(hash=0, chats=[], users=[], gifts=[
                types.StarGift(id=1, sticker=types.DocumentEmpty(id=0), stars=350, convert_stars=0,
                               title="Plush Pepe", upgrade_stars=100, availability_total=5000),
                types.StarGift(id=2, sticker=types.DocumentEmpty(id=0), stars=15, convert_stars=0),
            ])
        slug, num = req.slug.rsplit("-", 1)
        assert slug == "PlushPepe"
        if int(num) > self.issued:
            from telethon import errors
            raise errors.RPCError(None, "STARGIFT_SLUG_INVALID", 400)
        return unique(slug, int(num), self.issued)


def test_slug():
    assert title_to_slug("Plush Pepe") == "PlushPepe"
    assert title_to_slug("Durov's Cap") == "DurovsCap"
    assert title_to_slug("Jack-in-the-Box") == "JackInTheBox"
    assert title_to_slug("B-Day Candle") == "BDayCandle"


def test_pretty():
    assert is_pretty_number(7) and is_pretty_number(7777) and is_pretty_number(2500)
    assert is_pretty_number(12321) and is_pretty_number(1234)
    assert not is_pretty_number(1873)


def test_watcher(tmp_path):
    async def run():
        from giftbot import watcher as w
        w.config = type(w.config)(request_delay=0)
        db = Database(str(tmp_path / "t.db"))
        await db.connect()
        got = []

        async def on_upgrade(u):
            got.append(u)

        client = FakeClient()
        watcher = Watcher(client, db, on_upgrade)
        await watcher.refresh_collections()
        rows = await db.collections()
        assert [r["slug"] for r in rows] == ["PlushPepe"]
        await watcher.check_collection(rows[0])  # первый запуск — без рассылки
        assert got == []
        client.issued = 13
        await watcher.check_collection((await db.collections())[0])
        assert [u.num for u in got] == [11, 12, 13]
        u = got[0]
        assert (u.model, u.model_rarity, u.backdrop_rarity, u.symbol_rarity) == ("Gold", 8, 20, 5)
        assert u.owner_username == "ivan" and u.resale_stars == 999
        assert got[1].resale_stars is None
        await watcher.check_collection((await db.collections())[0])
        assert len(got) == 3
        assert len(await db.recent_upgrades()) == 3

        assert matches(Filters(), u)
        assert matches(Filters(collections=["Plush Pepe"], models=["gol"], max_model_rarity=10), u)
        assert not matches(Filters(max_backdrop_rarity=10), u)
        assert not matches(Filters(on_sale=True), got[1])
        assert not matches(Filters(max_num=5), u)
        assert "@ivan" in format_upgrade(u)

        await db.ensure_user(1)
        await db.set_filters(1, Filters(models=["x"]))
        assert (await db.get_filters(1)).models == ["x"]
        await db.close()

    asyncio.run(run())
