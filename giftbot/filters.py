from .models import Filters, Upgrade


def is_pretty_number(num: int) -> bool:
    """Красивые номера: до 100, круглые (1000, 2500), повторы (777, 4444), зеркальные (12321)."""
    s = str(num)
    if num <= 100:
        return True
    if len(set(s)) == 1:
        return True
    if s.rstrip("0") and len(s) - len(s.rstrip("0")) >= 2:
        return True
    if len(s) >= 4 and s == s[::-1]:
        return True
    if len(s) >= 4 and all(int(b) - int(a) == 1 for a, b in zip(s, s[1:])):
        return True
    return False


def _match_names(value: str | None, needles: list[str]) -> bool:
    if not needles:
        return True
    if not value:
        return False
    low = value.lower()
    return any(n.lower() in low for n in needles)


def _rarity_ok(value: int | None, limit: int | None) -> bool:
    if limit is None:
        return True
    return value is not None and value <= limit


def matches(f: Filters, u: Upgrade) -> bool:
    if f.collections and u.collection not in f.collections:
        return False
    if not _match_names(u.model, f.models):
        return False
    if not _match_names(u.backdrop, f.backdrops):
        return False
    if not _match_names(u.symbol, f.symbols):
        return False
    if not _rarity_ok(u.model_rarity, f.max_model_rarity):
        return False
    if not _rarity_ok(u.backdrop_rarity, f.max_backdrop_rarity):
        return False
    if not _rarity_ok(u.symbol_rarity, f.max_symbol_rarity):
        return False
    if f.max_num is not None and u.num > f.max_num:
        return False
    if f.pretty_num and not is_pretty_number(u.num):
        return False
    if f.with_username and not u.owner_username:
        return False
    if f.on_sale and not u.resale_stars:
        return False
    return True


def fmt_permille(p: int | None) -> str:
    if p is None:
        return "?"
    return f"{p / 10:g}%"


def describe(f: Filters) -> str:
    def lst(values: list[str]) -> str:
        return ", ".join(values) if values else "любые"

    rows = [
        f"🎁 Коллекции: {lst(f.collections) if f.collections else 'все'}",
        f"🧩 Модель: {lst(f.models)}"
        + (f" (≤ {fmt_permille(f.max_model_rarity)})" if f.max_model_rarity else ""),
        f"🎨 Фон: {lst(f.backdrops)}"
        + (f" (≤ {fmt_permille(f.max_backdrop_rarity)})" if f.max_backdrop_rarity else ""),
        f"🔣 Узор: {lst(f.symbols)}"
        + (f" (≤ {fmt_permille(f.max_symbol_rarity)})" if f.max_symbol_rarity else ""),
        f"🔢 Номер: {'≤ ' + str(f.max_num) if f.max_num else 'любой'}"
        + (", только красивые" if f.pretty_num else ""),
        f"👤 Только владельцы с @username: {'да' if f.with_username else 'нет'}",
        f"💰 Только выставленные на продажу: {'да' if f.on_sale else 'нет'}",
    ]
    return "\n".join(rows)
