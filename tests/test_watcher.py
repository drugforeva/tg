from conftest import FakeClient

from giftbot.pool import RateLimiter
from giftbot.watcher import Watcher


async def make_watcher(db, client, sink):
    async def on_upgrade(u):
        sink.append(u)

    w = Watcher(client, db, on_upgrade)
    await w.refresh_collections()
    return w, w.states[1]


async def test_first_run_then_new_upgrades(db, fast_config):
    got, client = [], FakeClient()
    w, st = await make_watcher(db, client, got)
    assert st.slug == "PlushPepe"
    assert await w.check_collection(st) == (0, False)  # первый запуск — без рассылки
    assert got == [] and st.last_num == 10

    client.issued, client.prices = 13, {11: 500}
    assert await w.check_collection(st) == (3, False)
    assert [u.num for u in got] == [11, 12, 13]
    u = got[0]
    assert (u.model, u.model_rarity, u.backdrop_rarity, u.symbol_rarity) == ("Gold", 8, 20, 5)
    assert u.owner_username == "ivan" and u.sender_name == "Petya" and u.resale_stars == 500
    assert u.value_usd == 4520 and got[1].resale_stars is None

    assert await w.check_collection(st) == (0, False)
    assert len(await db.recent_upgrades()) == 3
    assert (await db.collections())[0]["last_num"] == 13


async def test_catch_up_in_chunks(db, fast_config, monkeypatch):
    monkeypatch.setattr("giftbot.watcher.MAX_FETCH_PER_PASS", 5)
    got, client = [], FakeClient()
    w, st = await make_watcher(db, client, got)
    await w.check_collection(st)
    client.issued = 22
    assert await w.check_collection(st) == (5, True)
    assert await w.check_collection(st) == (5, True)
    assert await w.check_collection(st) == (2, False)
    assert [u.num for u in got] == list(range(11, 23))


async def test_missing_number_is_skipped_after_retries(db, fast_config):
    got, client = [], FakeClient()
    w, st = await make_watcher(db, client, got)
    await w.check_collection(st)
    client.issued, client.missing = 13, {11}
    assert await w.check_collection(st) == (0, False)
    assert await w.check_collection(st) == (0, False)
    found, _ = await w.check_collection(st)  # третья неудача — номер пропускаем
    assert found == 2 and [u.num for u in got] == [12, 13]


async def test_wrong_slug_is_discovered_via_market(db, fast_config):
    got, client = [], FakeClient(title="Plush  Pépé")  # из такого названия слаг не угадать
    w, st = await make_watcher(db, client, got)
    assert st.slug != "PlushPepe"
    await w.check_collection(st)
    assert st.slug == "PlushPepe" and st.last_num == 10
    assert (await db.collections())[0]["slug"] == "PlushPepe"
    # Найденный слаг переживает обновление списка коллекций.
    await w.refresh_collections()
    assert w.states[1].slug == "PlushPepe"


async def test_attributes_and_restart(db, fast_config):
    got, client = [], FakeClient()
    w, st = await make_watcher(db, client, got)
    await w.check_collection(st)
    from giftbot.parsing import parse_attributes

    resp = await client(__import__("telethon").tl.functions.payments.GetStarGiftUpgradeAttributesRequest(gift_id=1))
    await db.save_attributes(1, parse_attributes(resp.attributes))
    assert await db.attribute_options("model", [1]) == [(100, "Gold", 8)]
    assert await db.attribute_name("symbol", 200) == "Star"

    # Перезапуск: позиция берётся из базы, старое не рассылается повторно.
    w2 = Watcher(client, db, w.on_upgrade)
    await w2.load_states()
    assert w2.states[1].last_num == 10


def test_rate_limiter_backoff():
    rl = RateLimiter(3)
    rl.flood()
    assert rl.rate < 3
    for _ in range(2000):
        rl.success()
    assert rl.rate == 3
