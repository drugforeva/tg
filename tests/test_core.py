import time

import pytest
from telethon import errors

from giftbot.pool import ClientPool, PoolExhausted
from giftbot.tracker import diff, snapshot


class Account:
    def __init__(self, name, fail=None):
        self.name, self.fail, self.calls = name, fail, 0

    async def __call__(self, request):
        self.calls += 1
        if self.fail == "flood" and self.calls == 1:
            raise errors.FloodWaitError(request=None, capture=30)
        if self.fail == "dead":
            raise errors.AuthKeyUnregisteredError(request=None)
        return self.name


async def test_pool_switches_account_on_floodwait():
    a, b = Account("a", fail="flood"), Account("b")
    pool = ClientPool([a, b], rate=1000)
    results = [await pool(object()) for _ in range(3)]
    assert results == ["b", "b", "b"]  # «a» ушёл в FloodWait на 30 с, работает «b»
    assert pool.metrics.flood_waits == 1 and pool.members[0].limiter.rate < 1000


async def test_pool_drops_dead_account_and_alerts():
    alerts = []

    async def on_alert(text):
        alerts.append(text)

    pool = ClientPool([Account("dead", fail="dead"), Account("ok")], rate=1000, on_alert=on_alert)
    assert await pool(object()) == "ok"
    assert len(pool.members) == 1 and "AuthKeyUnregistered" in alerts[0]
    pool.members[0].client.fail = "dead"
    with pytest.raises(PoolExhausted):
        await pool(object())


def test_tracker_diff():
    base = {"owner_id": 1, "owner_username": "a", "resale_stars": None, "resale_ton": None, "collection": "X", "num": 1}
    listed = {**base, "resale_stars": 500}
    assert [k for k, _ in diff(base, listed)] == ["listed"]
    assert [k for k, _ in diff(listed, {**listed, "resale_stars": 400})] == ["price"]
    assert [k for k, _ in diff(listed, base)] == ["unlisted"]
    assert [k for k, _ in diff(listed, {**base, "owner_id": 2})] == ["sold"]
    assert [k for k, _ in diff(base, {**base, "owner_id": 2})] == ["owner"]
    assert [k for k, _ in diff(base, {**base, "burned": True})] == ["burned"]
    assert [k for k, _ in diff(base, snapshot(None))] == ["missing"]
    assert diff(base, base) == []


async def test_market_needed_follows_subscriptions(db, fast_config):
    from conftest import FakeClient

    from giftbot.market import Market
    from giftbot.models import Filters

    await db.upsert_collection(1, "Plush Pepe", "PlushPepe", 5000)
    await db.upsert_collection(2, "Durov's Cap", "DurovsCap", 1000)
    market = Market(FakeClient(), db, lambda u: None)
    await market.refresh_catalog()
    assert await market.needed() == set()  # подписок на продажи нет — ленты не опрашиваем
    await db.ensure_user(1)
    await db.create_subscription(1, "s", Filters(events=["listing"], collections=["Durov's Cap"]))
    market._needed = (float("-inf"), set())
    assert await market.needed() == {2}
    await db.create_subscription(1, "all", Filters(events=["upgrade", "listing"]))
    market._needed = (float("-inf"), set())
    assert await market.needed() == {1, 2}
    assert market.floor_of(1) == 9000 and time.time()
