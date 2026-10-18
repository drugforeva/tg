from giftbot.filters import fmt_one_in, is_pretty_number, matches, name_matches
from giftbot.models import Filters, Upgrade
from giftbot.parsing import title_to_slug
from giftbot.render import upgrade_card, upgrade_line


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


def test_new_filters():
    from giftbot.filters import number_matches, parse_numbers

    u = make(owner_id=42, sender_id=7, sender_username="petya")
    assert parse_numbers("1, 69 777;1000-1100, abc, 5—9") == ["1", "69", "777", "1000-1100", "5-9"]
    assert number_matches(1050, ["1000-1100"]) and number_matches(69, ["69"]) and not number_matches(70, ["69"])
    assert matches(Filters(numbers=["1234"]), u) and not matches(Filters(numbers=["1-100"]), u)
    assert matches(Filters(owners=["@IVAN"]), u) and matches(Filters(owners=["petya"]), u)
    assert matches(Filters(owners=["42"]), u) and not matches(Filters(owners=["someone"]), u)
    assert not matches(Filters(exclude_models=["gold"]), u) and matches(Filters(exclude_models=["silver"]), u)
    assert not matches(Filters(exclude_symbols=["st*"]), u)
    assert not matches(Filters(events=["listing"]), u)
    listing = make(event="listing", resale_stars=700, floor_stars=1000)
    assert listing.discount == 30
    assert matches(Filters(events=["listing"], min_discount=30), listing)
    assert not matches(Filters(events=["listing"], min_discount=31), listing)
    assert not matches(Filters(min_discount=5), make(resale_stars=700))  # флор неизвестен


def test_sanitize_untrusted_filters():
    f = Filters.sanitize(
        {
            "events": ["listing", "evil"],
            "collections": ["A", 5, ""],
            "max_num": "100",
            "max_model_rarity": 5000,
            "min_discount": -3,
            "pretty_num": "yes",
            "unknown": 1,
        }
    )
    assert f.events == ["listing"] and f.collections == ["A", "5"] and f.max_num == 100
    assert f.max_model_rarity is None and f.min_discount is None and f.pretty_num is True
    assert Filters.sanitize("garbage") == Filters()


def test_parse_slug():
    from giftbot.parsing import parse_slug

    assert parse_slug("https://t.me/nft/PlushPepe-123") == "PlushPepe-123"
    assert parse_slug("смотри t.me/nft/DurovsCap-5 круто") == "DurovsCap-5"
    assert parse_slug("PlushPepe-7") == "PlushPepe-7"
    assert parse_slug("просто текст") is None


def test_listing_card():
    from giftbot.render import listing_card

    card = listing_card(make(event="listing", resale_stars=6000, floor_stars=9000), ["Снайпер"])
    assert "выставлен на продажу" in card and "6 000 ⭐" in card and "−33% от флора (9 000 ⭐)" in card
    assert "Продавец: @ivan" in card and "Снайпер" in card


def test_chart_renders_png():
    from giftbot.charts import collection_chart

    png = collection_chart("Plush Pepe", [(0, 5), (3600, 9)], [(0, 1000), (600, 1200)], "Europe/Moscow")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert collection_chart("x", [], [(0, None)], "UTC") is None
