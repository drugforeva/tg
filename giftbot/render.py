"""Тексты сообщений о подарках."""

import html

from .filters import fmt_one_in, fmt_permille
from .models import Upgrade


def esc(text: str | None) -> str:
    return html.escape(text or "")


def _person(user_id: int | None, name: str | None, username: str | None) -> str | None:
    if username:
        return f"@{esc(username)}"
    if user_id and name:
        return f'<a href="tg://user?id={user_id}">{esc(name)}</a>'
    return esc(name) or None


def owner_text(u: Upgrade) -> str:
    person = _person(u.owner_id, u.owner_name, u.owner_username)
    if person:
        return person
    if u.owner_address:
        return f"<code>{esc(u.owner_address)}</code> (TON)"
    return "скрыт"


def price_text(u: Upgrade) -> str | None:
    parts = []
    if u.resale_stars:
        parts.append(f"{u.resale_stars:,} ⭐".replace(",", " "))
    if u.resale_ton:
        parts.append(f"{u.resale_ton / 1e9:g} TON")
    return " / ".join(parts) or None


def _attr(label: str, name: str | None, rarity: int | None) -> str:
    if not name:
        return ""
    return f"{label}: <b>{esc(name)}</b> ({fmt_permille(rarity)})\n"


def upgrade_card(u: Upgrade, subscriptions: list[str] | None = None) -> str:
    title = f'<b><a href="{u.link}">{esc(u.collection)} #{u.num}</a></b>'
    head = f"🆕 {title} · выпущено {u.issued:,}/{u.total:,}".replace(",", " ")
    if u.crafted:
        head += " · 🛠 крафт"
    text = (
        head
        + "\n\n"
        + _attr("🧩 Модель", u.model, u.model_rarity)
        + _attr("🎨 Фон", u.backdrop, u.backdrop_rarity)
        + _attr("🔣 Узор", u.symbol, u.symbol_rarity)
    )
    if u.one_in:
        text += f"💎 Сочетание: <b>{fmt_one_in(u.one_in)}</b>\n"
    text += f"👤 Владелец: {owner_text(u)}\n"
    if sender := _person(u.sender_id, u.sender_name, u.sender_username):
        text += f"🎁 Подарил: {sender}\n"
    if price := price_text(u):
        text += f"💰 Продаётся: <b>{price}</b>\n"
    if u.value_usd:
        text += f"💵 Оценка: ≈ ${u.value_usd / 100:,.2f}\n".replace(",", " ")
    if subscriptions:
        text += f"📌 {esc(', '.join(subscriptions))}\n"
    return text


def upgrade_line(u: Upgrade) -> str:
    attrs = " · ".join(esc(x) for x in (u.model, u.backdrop, u.symbol) if x)
    line = f'• <a href="{u.link}">{esc(u.collection)} #{u.num}</a> — {attrs}'
    if u.one_in:
        line += f" · {fmt_one_in(u.one_in)}"
    if price := price_text(u):
        line += f" · 💰 {price}"
    return line


def upgrade_list(title: str, items: list[Upgrade], limit: int = 25, footer: str = "") -> str:
    lines = [upgrade_line(u) for u in items[:limit]]
    text = f"{title}\n\n" + "\n".join(lines)
    if len(items) > limit:
        text += f"\n\n… и ещё {len(items) - limit}."
    if footer:
        text += f"\n\n{footer}"
    return text
