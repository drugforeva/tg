from fnmatch import fnmatchcase

from .models import Filters, Upgrade


def is_pretty_number(num: int) -> bool:
    """Красивые номера: до 100, повторы (777), круглые (2500), зеркальные (12321), лесенка (1234)."""
    s = str(num)
    if num <= 100:
        return True
    if len(set(s)) == 1:
        return True
    if len(s) - len(s.rstrip("0")) >= 2:
        return True
    if len(s) >= 4 and s == s[::-1]:
        return True
    if len(s) >= 4 and all(int(b) - int(a) == 1 for a, b in zip(s, s[1:])):
        return True
    return False


def name_matches(value: str | None, patterns: list[str]) -> bool:
    """Точное совпадение без учёта регистра; * — любой текст (например *Pepe*)."""
    if not patterns:
        return True
    if not value:
        return False
    v = value.casefold()
    return any(fnmatchcase(v, p.casefold()) for p in patterns)


def _rarity_ok(value: int | None, limit: int | None) -> bool:
    if limit is None:
        return True
    return value is not None and value <= limit


def matches(f: Filters, u: Upgrade) -> bool:
    if f.collections and u.collection not in f.collections:
        return False
    if u.collection in f.exclude_collections:
        return False
    if not name_matches(u.model, f.models):
        return False
    if not name_matches(u.backdrop, f.backdrops):
        return False
    if not name_matches(u.symbol, f.symbols):
        return False
    if not _rarity_ok(u.model_rarity, f.max_model_rarity):
        return False
    if not _rarity_ok(u.backdrop_rarity, f.max_backdrop_rarity):
        return False
    if not _rarity_ok(u.symbol_rarity, f.max_symbol_rarity):
        return False
    if f.min_one_in and (u.one_in or 0) < f.min_one_in:
        return False
    if f.max_num is not None and u.num > f.max_num:
        return False
    if f.pretty_num and not is_pretty_number(u.num):
        return False
    if f.with_username and not u.owner_username:
        return False
    if (f.on_sale or f.max_price) and not (u.resale_stars or u.resale_ton):
        return False
    if f.max_price and (not u.resale_stars or u.resale_stars > f.max_price):
        return False
    return True


def fmt_permille(p: int | None) -> str:
    if p is None:
        return "?"
    return f"{p / 10:g}%"


def fmt_one_in(n: int | None) -> str:
    if not n:
        return "?"
    if n >= 1_000_000:
        return f"1 из {n / 1_000_000:.3g} млн"
    if n >= 1_000:
        return f"1 из {n / 1_000:.3g} тыс."
    return f"1 из {n}"


def describe(f: Filters) -> str:
    def lst(values: list[str], empty: str = "любые") -> str:
        return ", ".join(values) if values else empty

    def rar(limit: int | None) -> str:
        return f" (≤ {fmt_permille(limit)})" if limit else ""

    rows = [f"🎁 Коллекции: {lst(f.collections, 'все')}"]
    if f.exclude_collections:
        rows.append(f"🚫 Кроме: {lst(f.exclude_collections)}")
    rows += [
        f"🧩 Модель: {lst(f.models)}{rar(f.max_model_rarity)}",
        f"🎨 Фон: {lst(f.backdrops)}{rar(f.max_backdrop_rarity)}",
        f"🔣 Узор: {lst(f.symbols)}{rar(f.max_symbol_rarity)}",
    ]
    if f.min_one_in:
        rows.append(f"💎 Сочетание: не чаще {fmt_one_in(f.min_one_in)}")
    num = f"≤ {f.max_num}" if f.max_num else "любой"
    rows.append(f"🔢 Номер: {num}{', только красивые' if f.pretty_num else ''}")
    if f.with_username:
        rows.append("👤 Только владельцы с @username")
    if f.max_price:
        rows.append(f"💰 На продаже до {f.max_price} ⭐")
    elif f.on_sale:
        rows.append("💰 Только выставленные на продажу")
    return "\n".join(rows)
