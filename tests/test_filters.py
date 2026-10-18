from giftbot.filters import fmt_one_in, is_pretty_number, matches, name_matches
from giftbot.models import Filters, Upgrade
from giftbot.render import upgrade_card, upgrade_line
from giftbot.watcher import title_to_slug


def make(**kw):
    base = dict(
        slug="PlushPepe-1234",
        collection="Plush Pepe",
        gift_id=1,
        num=1234,
        issued=1234,
        total=5000,
        model="Gold",
        model_rarity=8,
        backdrop="Black",
        backdrop_rarity=20,
        symbol="Star",
        symbol_rarity=5,
        owner_id=42,
        owner_name="Ivan",
        owner_username="ivan",
    )
    base.update(kw)
    return Upgrade(**base)


def test_slug():
    assert title_to_slug("Plush Pepe") == "PlushPepe"
    assert title_to_slug("Durov's Cap") == "DurovsCap"
    assert title_to_slug("Jack-in-the-Box") == "JackInTheBox"
    assert title_to_slug("B-Day Candle") == "BDayCandle"


def test_pretty_numbers():
    for n in (7, 100, 777, 7777, 2500, 10000, 12321, 1234, 45678):
        assert is_pretty_number(n), n
    for n in (101, 1873, 2501, 1243):
        assert not is_pretty_number(n), n


def test_name_matching():
    assert name_matches("Gold", ["gold"])
    assert not name_matches("Golden Hour", ["gold"])
    assert name_matches("Golden Hour", ["gold*"])
    assert name_matches("Pepe Frog", ["*frog"])
    assert name_matches("x", [])
    assert not name_matches(None, ["x"])


def test_one_in():
    u = make()
    assert u.one_in == round(1 / (0.008 * 0.02 * 0.005))  # 1 из 1.25 млн
    assert fmt_one_in(u.one_in) == "1 из 1.25 млн"
    assert make(symbol_rarity=None).one_in is None


def test_matches():
    u = make()
    assert matches(Filters(), u)
    assert matches(Filters(collections=["Plush Pepe"], models=["GOLD"], max_model_rarity=10), u)
    assert not matches(Filters(exclude_collections=["Plush Pepe"]), u)
    assert not matches(Filters(max_backdrop_rarity=10), u)
    assert matches(Filters(min_one_in=1_000_000), u)
    assert not matches(Filters(min_one_in=10_000_000), u)
    assert not matches(Filters(max_num=1000), u)
    assert matches(Filters(pretty_num=True), u)
    assert not matches(Filters(with_username=True), make(owner_username=None))
    assert not matches(Filters(on_sale=True), u)
    assert matches(Filters(on_sale=True), make(resale_ton=10**9))
    assert matches(Filters(max_price=1000), make(resale_stars=900))
    assert not matches(Filters(max_price=1000), make(resale_stars=1100))
    assert not matches(Filters(max_price=1000), make(resale_ton=10**9))


def test_filters_roundtrip_ignores_unknown_keys():
    f = Filters.from_dict({"models": ["a"], "unknown": 1})
    assert f.models == ["a"] and Filters.from_dict(f.to_dict()) == f


def test_render_escapes_and_shows_everything():
    u = make(
        collection="<b>x</b>",
        resale_stars=12000,
        resale_ton=2_500_000_000,
        value_usd=4520,
        sender_id=7,
        sender_name="Petya",
        crafted=True,
    )
    card = upgrade_card(u, ["Основная"])
    assert "&lt;b&gt;x&lt;/b&gt;" in card
    assert "@ivan" in card and "Petya" in card and "12 000 ⭐ / 2.5 TON" in card
    assert "$45.20" in card and "крафт" in card and "Основная" in card
    assert "1 из 1.25 млн" in upgrade_line(u)
