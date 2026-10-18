"""Тексты сообщений о подарках."""

import html

from .filters import fmt_one_in, fmt_permille, fmt_stars
from .models import Upgrade


def esc(text: str | None) -> str:
    return html.escape(text or "")


def _person(user_id: int | None, name: str | None, username: str | None) -> str | None:
    if username:
        return f"@{esc(username)}"
    if user_id and name:
        return f'<a href="tg://user?id={user_id}">{esc(name)}</a>'
    return esc(name) or None


def owner_text(u) -> str:
    """u — Upgrade или словарь-снимок с полями владельца."""
    get = u.get if isinstance(u, dict) else lambda k: getattr(u, k)
    person = _person(get("owner_id"), get("owner_name"), get("owner_username"))
    if person:
        return person
    if get("owner_address"):
        return f"<code>{esc(get('owner_address'))}</code> (TON)"
    return "скрыт"


def price_text(u) -> str | None:
    stars, ton = (u if isinstance(u, tuple) else (u.resale_stars, u.resale_ton))[:2]
    parts = []
    if stars:
        parts.append(fmt_stars(stars))
    if ton:
        parts.append(f"{ton / 1e9:g} TON")
    return " / ".join(parts) or None


def discount_text(u: Upgrade) -> str | None:
    d = u.discount
    if d is None:
        return None
    if d > 0:
        return f"📉 −{d}% от флора ({fmt_stars(u.floor_stars)})"
    if d < 0:
        return f"📈 +{-d}% к флору ({fmt_stars(u.floor_stars)})"
    return f"= флор ({fmt_stars(u.floor_stars)})"


def _attr(label: str, name: str | None, rarity: int | None) -> str:
    if not name:
        return ""
    return f"{label}: <b>{esc(name)}</b> ({fmt_permille(rarity)})\n"


def _attributes(u: Upgrade) -> str:
    text = (
        _attr("🧩 Модель", u.model, u.model_rarity)
        + _attr("🎨 Фон", u.backdrop, u.backdrop_rarity)
        + _attr("🔣 Узор", u.symbol, u.symbol_rarity)
    )
    if u.one_in:
        text += f"💎 Сочетание: <b>{fmt_one_in(u.one_in)}</b>\n"
    return text


def upgrade_card(u: Upgrade, subscriptions: list[str] | None = None) -> str:
    if u.event == "listing":
        return listing_card(u, subscriptions)
    title = f'<b><a href="{u.link}">{esc(u.collection)} #{u.num}</a></b>'
    head = f"🆕 {title} · выпущено {u.issued:,}/{u.total:,}".replace(",", " ")
    if u.crafted:
        head += " · 🛠 крафт"
    text = head + "\n\n" + _attributes(u)
    text += f"👤 Владелец: {owner_text(u)}\n"
    if sender := _person(u.sender_id, u.sender_name, u.sender_username):
        text += f"🎁 Подарил: {sender}\n"
    if price := price_text(u):
        text += f"💰 Продаётся: <b>{price}</b>\n"
        if discount := discount_text(u):
            text += f"{discount}\n"
    elif u.floor_stars:
        text += f"🏷 Флор коллекции: {fmt_stars(u.floor_stars)}\n"
    if u.value_usd:
        text += f"💵 Оценка: ≈ ${u.value_usd / 100:,.2f}\n".replace(",", " ")
    if subscriptions:
        text += f"📌 {esc(', '.join(subscriptions))}\n"
    return text


def listing_card(u: Upgrade, subscriptions: list[str] | None = None) -> str:
    title = f'<b><a href="{u.link}">{esc(u.collection)} #{u.num}</a></b>'
    text = f"🏷 {title} выставлен на продажу\n\n💰 Цена: <b>{price_text(u) or '?'}</b>\n"
    if discount := discount_text(u):
        text += f"{discount}\n"
    text += "\n" + _attributes(u)
    text += f"👤 Продавец: {owner_text(u)}\n"
    if u.value_usd:
        text += f"💵 Оценка: ≈ ${u.value_usd / 100:,.2f}\n".replace(",", " ")
    if subscriptions:
        text += f"📌 {esc(', '.join(subscriptions))}\n"
    return text


def upgrade_line(u: Upgrade) -> str:
    attrs = " · ".join(esc(x) for x in (u.model, u.backdrop, u.symbol) if x)
    icon = "🏷 " if u.event == "listing" else ""
    line = f'• {icon}<a href="{u.link}">{esc(u.collection)} #{u.num}</a> — {attrs}'
    if u.one_in:
        line += f" · {fmt_one_in(u.one_in)}"
    if price := price_text(u):
        line += f" · 💰 {price}"
        if u.discount and u.discount > 0:
            line += f" (−{u.discount}%)"
    return line


def upgrade_list(title: str, items: list[Upgrade], limit: int = 25, footer: str = "") -> str:
    lines = [upgrade_line(u) for u in items[:limit]]
    text = f"{title}\n\n" + "\n".join(lines)
    if len(items) > limit:
        text += f"\n\n… и ещё {len(items) - limit}."
    if footer:
        text += f"\n\n{footer}"
    return text


def track_changes_text(slug: str, u: Upgrade | None, new: dict, changes: list[tuple[str, dict]]) -> str:
    name = f"{new['collection']} #{new['num']}" if "collection" in new else slug
    lines = [f'👁 <b><a href="https://t.me/nft/{esc(slug)}">{esc(name)}</a></b>']
    for kind, info in changes:
        if kind == "missing":
            lines.append("⚠️ Подарок больше недоступен")
        elif kind == "back":
            lines.append("✅ Подарок снова доступен")
        elif kind == "burned":
            lines.append("🔥 Подарок сожжён")
        elif kind == "sold":
            lines.append(
                f"💸 Продан за {price_text(info['price']) or '?'}: {owner_text(info['old'])} → {owner_text(new)}"
            )
        elif kind == "owner":
            lines.append(f"👤 Новый владелец: {owner_text(new)} (был {owner_text(info['old'])})")
        elif kind == "listed":
            lines.append(f"🏷 Выставлен на продажу: <b>{price_text(info['price'])}</b>")
        elif kind == "unlisted":
            lines.append("❎ Снят с продажи")
        elif kind == "price":
            lines.append(f"💱 Цена: {price_text(info['old'])} → <b>{price_text(info['price'])}</b>")
    return "\n".join(lines)
