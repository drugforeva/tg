"""Отчёты: последние события, топы, статистика, карточка коллекции, ежедневная сводка."""

import time

from .config import config
from .db import Database
from .filters import fmt_permille, fmt_stars, matches
from .render import esc, upgrade_list

DAY = 86400


async def recent_text(db: Database, chat_id: int, limit: int) -> str:
    subs = [s for s in await db.subscriptions(chat_id) if s.enabled]
    events = await db.recent_upgrades(3000)
    if any("listing" in s.filters.events for s in subs):
        events = sorted(events + await db.recent_listings(1000), key=lambda u: u.detected_at, reverse=True)
    found = [u for u in events if not subs or any(matches(s.filters, u) for s in subs)][:limit]
    if not found:
        return "Пока ничего не нашлось по вашим подпискам."
    return upgrade_list(f"📜 <b>Последнее по вашим подпискам</b> ({len(found)})", found, limit)


async def top_text(db: Database) -> str:
    found = await db.rarest_upgrades(int(time.time()) - DAY, 15)
    if not found:
        return "За сутки пока нет данных."
    return upgrade_list("🏆 <b>Самые редкие сочетания за 24 часа</b>", found, 15)


async def deals_text(db: Database) -> str:
    found = [u for u in await db.best_deals(int(time.time()) - DAY, 15) if (u.discount or 0) > 0]
    if not found:
        return (
            "🔥 За сутки выгодных лотов не замечено.\n\n"
            "Лента продаж собирается для коллекций, на которые есть подписки с событием «🏷 Продажи»."
        )
    return upgrade_list("🔥 <b>Самые выгодные лоты за 24 часа</b> (ниже флора)", found, 15)


def _ago(ts: float | None) -> str:
    if not ts:
        return "—"
    seconds = int(time.time() - ts)
    if seconds < 60:
        return f"{seconds} с назад"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    return f"{seconds // 3600} ч назад"


async def stats_text(db: Database, app, is_admin: bool) -> str:
    s = await db.stats()
    top = await db.top_collections(int(time.time()) - DAY, 10)
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"✨ Улучшений: {s['upgrades_1h']} за час, {s['upgrades_24h']} за сутки",
        f"🏷 Новых лотов на продаже за сутки: {s['listings_24h']}",
        f"🎁 Коллекций отслеживается: {s['collections']}",
    ]
    if top:
        lines += ["", "<b>Самые активные за сутки:</b>"]
        lines += [f"{i}. {esc(title)} — {count}" for i, (title, count) in enumerate(top, 1)]
    if is_admin:
        m = app.watcher.metrics
        uptime = int(time.time() - m.started_at)
        hot = sum(1 for st in app.watcher.states.values() if st.interval <= config.poll_min * 2)
        pool = app.pool
        lines += [
            "",
            "<b>Служебное</b>",
            f"👥 Чатов: {s['users']} (активных {s['active']}, премиум {s['premium']}), подписок: {s['subscriptions']}",
            f"📢 Каналов: {s['channels']}, слежка за подарками: {s['tracked']}",
            f"⏱ Аптайм: {uptime // 3600} ч {uptime % 3600 // 60} мин",
        ]
        if pool is not None and hasattr(pool, "members"):
            lines.append(
                f"📡 Аккаунтов: {len(pool.members)}, запросов: {pool.metrics.requests}, "
                f"скорость {pool.rate:.2f}/с, FloodWait: {pool.metrics.flood_waits}"
            )
            lines += [f"⛔️ Отключён аккаунт {esc(d)}" for d in pool.metrics.dead]
        lines += [
            f"🔥 Активных коллекций: {hot}",
            f"🆕 Улучшений за запуск: {m.found}, последнее {_ago(m.last_found_at)}",
        ]
        if app.market is not None:
            lines.append(f"🏷 Лотов за запуск: {app.market.found}, лент в работе: {len(app.market.seen)}")
        if app.tracker is not None:
            lines.append(f"👁 Проверок отслеживаемых подарков: {app.tracker.checked}")
        lines.append(f"✉️ Отправлено: {app.delivery.sent}, в очереди чатов: {len(app.delivery.pending)}")
        if m.fatal:
            lines.append(f"⛔️ Наблюдатель остановлен: {m.fatal}")
        if m.last_error:
            lines.append(f"⚠️ Последняя ошибка: {esc(m.last_error[:200])}")
    return "\n".join(lines)


async def collection_text(db: Database, row) -> str:
    now = int(time.time())
    gift_id, total = row["gift_id"], row["total"] or 0
    issued = row["last_num"] or 0
    h1 = await db.upgrades_count(gift_id, now - 3600)
    d1 = await db.upgrades_count(gift_id, now - DAY)
    d7 = await db.upgrades_count(gift_id, now - 7 * DAY)
    lines = [f"📈 <b>{esc(row['title'])}</b>", ""]
    if total:
        lines.append(f"✨ Улучшено: {issued:,} из {total:,} ({issued * 100 / total:.1f}%)".replace(",", " "))
    else:
        lines.append(f"✨ Улучшено: {issued:,}".replace(",", " "))
    lines.append(f"⚡️ Темп: {h1} за час, {d1} за сутки, {d7} за неделю (что увидел бот)")
    if d1 and total > issued:
        days = (total - issued) / d1
        lines.append(f"⏳ При таком темпе лимит закончится через ~{days:.0f} дн.")
    if row["floor"]:
        lines.append(f"🏷 Флор: {fmt_stars(row['floor'])}, лотов на маркете: {row['resale_count'] or 0}")
    models = await db.top_models(gift_id, now - DAY)
    if models:
        lines += ["", "<b>Частые модели за сутки:</b>"]
        lines += [f"• {esc(name)} ({fmt_permille(r)}) — {count}" for name, count, r in models]
    lines.append(f"\n🔗 https://t.me/nft/{esc(row['slug'])}-1")
    return "\n".join(lines)


async def daily_text(db: Database) -> str:
    s = await db.stats()
    top = await db.top_collections(int(time.time()) - DAY, 5)
    rare = await db.rarest_upgrades(int(time.time()) - DAY, 5)
    deals = [u for u in await db.best_deals(int(time.time()) - DAY, 5) if (u.discount or 0) > 0]
    lines = [
        "📰 <b>Сводка за сутки</b>",
        "",
        f"✨ Улучшений: {s['upgrades_24h']}, 🏷 новых лотов: {s['listings_24h']}",
    ]
    if top:
        lines += ["", "<b>Самые активные коллекции:</b>"]
        lines += [f"{i}. {esc(title)} — {count}" for i, (title, count) in enumerate(top, 1)]
    text = "\n".join(lines)
    if rare:
        text += "\n\n" + upgrade_list("<b>🏆 Самые редкие:</b>", rare, 5)
    if deals:
        text += "\n\n" + upgrade_list("<b>🔥 Самые выгодные лоты:</b>", deals, 5)
    return text
