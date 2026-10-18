import json
import time
from dataclasses import fields

import aiosqlite

from .models import Filters, Subscription, Upgrade

_SQL_TYPES = {int: "INTEGER", str: "TEXT", bool: "INTEGER"}


def _event_columns() -> str:
    """Колонки таблиц событий строятся из полей Upgrade, чтобы не разъезжались."""
    cols = []
    for f in fields(Upgrade):
        base = f.type if isinstance(f.type, type) else {"int | None": int, "str | None": str}.get(str(f.type), str)
        cols.append(f"{f.name} {_SQL_TYPES.get(base, 'TEXT')}")
    return ",\n    ".join(cols)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,  -- id чата: личка или группа
    active INTEGER NOT NULL DEFAULT 1,
    filters TEXT NOT NULL DEFAULT '{{}}',  -- устарело, перенесено в subscriptions
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    filters TEXT NOT NULL DEFAULT '{{}}',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS subscriptions_chat ON subscriptions(chat_id);
CREATE TABLE IF NOT EXISTS collections (
    gift_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    last_num INTEGER,
    total INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attributes (
    gift_id INTEGER NOT NULL,
    kind TEXT NOT NULL,  -- model / backdrop / symbol
    attr_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    rarity INTEGER,
    PRIMARY KEY (gift_id, kind, attr_id)
);
CREATE TABLE IF NOT EXISTS upgrades (
    slug TEXT PRIMARY KEY,
    collection TEXT NOT NULL,
    gift_id INTEGER NOT NULL,
    num INTEGER NOT NULL,
    issued INTEGER NOT NULL,
    total INTEGER NOT NULL,
    model TEXT, model_rarity INTEGER,
    backdrop TEXT, backdrop_rarity INTEGER,
    symbol TEXT, symbol_rarity INTEGER,
    owner_id INTEGER, owner_name TEXT, owner_username TEXT, owner_address TEXT,
    resale_stars INTEGER,
    detected_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS upgrades_detected ON upgrades(detected_at DESC);
CREATE TABLE IF NOT EXISTS listings (
    key TEXT PRIMARY KEY,  -- slug + цена: смена цены — новое выставление
    {_event_columns()}
);
CREATE INDEX IF NOT EXISTS listings_detected ON listings(detected_at DESC);
CREATE TABLE IF NOT EXISTS floors (
    gift_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    floor INTEGER,
    resale_count INTEGER
);
CREATE INDEX IF NOT EXISTS floors_gift ON floors(gift_id, ts);
CREATE TABLE IF NOT EXISTS tracked (
    chat_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, slug)
);
CREATE TABLE IF NOT EXISTS gift_states (
    slug TEXT PRIMARY KEY,
    snapshot TEXT,
    checked_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channels (
    channel_id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    days INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    refunded INTEGER NOT NULL DEFAULT 0
);
"""

# Колонки, добавленные после первой версии: (таблица, колонка, объявление)
MIGRATIONS = [
    ("users", "digest_window", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "daily_hour", "INTEGER"),
    ("users", "daily_sent", "TEXT"),
    ("users", "premium_until", "INTEGER NOT NULL DEFAULT 0"),
    ("subscriptions", "target_id", "INTEGER"),
    ("collections", "attrs_updated", "INTEGER"),
    ("collections", "floor", "INTEGER"),
    ("collections", "resale_count", "INTEGER"),
    ("upgrades", "sender_id", "INTEGER"),
    ("upgrades", "sender_name", "TEXT"),
    ("upgrades", "sender_username", "TEXT"),
    ("upgrades", "resale_ton", "INTEGER"),
    ("upgrades", "value_usd", "INTEGER"),
    ("upgrades", "crafted", "INTEGER NOT NULL DEFAULT 0"),
    ("upgrades", "event", "TEXT NOT NULL DEFAULT 'upgrade'"),
    ("upgrades", "floor_stars", "INTEGER"),
]


def _sub(row) -> Subscription:
    return Subscription(
        id=row["id"],
        chat_id=row["chat_id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        filters=Filters.from_dict(json.loads(row["filters"])),
        target_id=row["target_id"],
    )


def _upgrade(row) -> Upgrade:
    data = dict(row)
    data["crafted"] = bool(data.get("crafted"))
    return Upgrade(**{k: v for k, v in data.items() if k in Upgrade.__dataclass_fields__})


def listing_key(u: Upgrade) -> str:
    return f"{u.slug}:{u.resale_stars or 0}:{u.resale_ton or 0}"


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        for table, column, decl in MIGRATIONS:
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            if column not in {r["name"] for r in await cur.fetchall()}:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        # Фильтры из первой версии (по одному набору на пользователя) -> подписки.
        cur = await self.conn.execute(
            """SELECT user_id, filters, created_at FROM users
               WHERE filters != '{}' AND user_id NOT IN (SELECT chat_id FROM subscriptions)"""
        )
        for row in await cur.fetchall():
            await self.conn.execute(
                "INSERT INTO subscriptions(chat_id, name, filters, created_at) VALUES (?, ?, ?, ?)",
                (row["user_id"], "Основная", row["filters"], row["created_at"]),
            )
        await self.conn.execute("UPDATE users SET filters = '{}' WHERE filters != '{}'")

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, *args):
        cur = await self.conn.execute(sql, args)
        row = await cur.fetchone()
        return row[0] if row else None

    async def _all(self, sql: str, *args) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, args)
        return list(await cur.fetchall())

    async def _exec(self, sql: str, *args) -> int:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.rowcount

    async def backup(self, path: str) -> None:
        """Согласованная копия базы (можно делать на ходу)."""
        await self.conn.execute("VACUUM INTO ?", (path,))

    # --- чаты ---

    async def ensure_user(self, chat_id: int) -> bool:
        """Создаёт чат при первом обращении. Возвращает True, если он новый."""
        return (
            await self._exec(
                "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)", chat_id, int(time.time())
            )
            > 0
        )

    async def set_active(self, chat_id: int, active: bool) -> None:
        await self._exec("UPDATE users SET active = ? WHERE user_id = ?", int(active), chat_id)

    async def is_active(self, chat_id: int) -> bool:
        return bool(await self._one("SELECT active FROM users WHERE user_id = ?", chat_id))

    async def active_chats(self) -> list[int]:
        return [r[0] for r in await self._all("SELECT user_id FROM users WHERE active = 1")]

    async def get_window(self, chat_id: int) -> int:
        return await self._one("SELECT digest_window FROM users WHERE user_id = ?", chat_id) or 0

    async def set_window(self, chat_id: int, seconds: int) -> None:
        await self._exec("UPDATE users SET digest_window = ? WHERE user_id = ?", seconds, chat_id)

    async def get_daily(self, chat_id: int) -> int | None:
        return await self._one("SELECT daily_hour FROM users WHERE user_id = ?", chat_id)

    async def set_daily(self, chat_id: int, hour: int | None) -> None:
        await self._exec("UPDATE users SET daily_hour = ? WHERE user_id = ?", hour, chat_id)

    async def daily_due(self, hour: int, today: str) -> list[int]:
        rows = await self._all(
            """SELECT user_id FROM users WHERE active = 1 AND daily_hour = ?
               AND COALESCE(daily_sent, '') != ?""",
            hour,
            today,
        )
        return [r[0] for r in rows]

    async def mark_daily_sent(self, chat_id: int, today: str) -> None:
        await self._exec("UPDATE users SET daily_sent = ? WHERE user_id = ?", today, chat_id)

    # --- премиум ---

    async def premium_until(self, chat_id: int) -> int:
        return await self._one("SELECT premium_until FROM users WHERE user_id = ?", chat_id) or 0

    async def add_payment(self, charge_id: str, chat_id: int, user_id: int, amount: int, days: int) -> int:
        """Продлевает премиум и записывает платёж. Возвращает новый срок окончания."""
        await self.ensure_user(chat_id)
        now = int(time.time())
        until = max(await self.premium_until(chat_id), now) + days * 86400
        await self.conn.execute(
            "INSERT OR IGNORE INTO payments(charge_id, chat_id, user_id, amount, days, created_at) VALUES (?,?,?,?,?,?)",
            (charge_id, chat_id, user_id, amount, days, now),
        )
        await self.conn.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (until, chat_id))
        await self.conn.commit()
        return until

    async def get_payment(self, charge_id: str):
        cur = await self.conn.execute("SELECT * FROM payments WHERE charge_id = ?", (charge_id,))
        return await cur.fetchone()

    async def refund_payment(self, charge_id: str) -> None:
        payment = await self.get_payment(charge_id)
        if not payment or payment["refunded"]:
            return
        await self.conn.execute("UPDATE payments SET refunded = 1 WHERE charge_id = ?", (charge_id,))
        await self.conn.execute(
            "UPDATE users SET premium_until = MAX(0, premium_until - ?) WHERE user_id = ?",
            (payment["days"] * 86400, payment["chat_id"]),
        )
        await self.conn.commit()

    # --- подписки ---

    async def subscriptions(self, chat_id: int) -> list[Subscription]:
        return [_sub(r) for r in await self._all("SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY id", chat_id)]

    async def get_subscription(self, sub_id: int) -> Subscription | None:
        rows = await self._all("SELECT * FROM subscriptions WHERE id = ?", sub_id)
        return _sub(rows[0]) if rows else None

    async def create_subscription(self, chat_id: int, name: str, f: Filters | None = None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO subscriptions(chat_id, name, filters, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, name, json.dumps((f or Filters()).to_dict(), ensure_ascii=False), int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def save_subscription(self, sub: Subscription) -> None:
        await self._exec(
            "UPDATE subscriptions SET name = ?, enabled = ?, filters = ?, target_id = ? WHERE id = ?",
            sub.name,
            int(sub.enabled),
            json.dumps(sub.filters.to_dict(), ensure_ascii=False),
            sub.target_id,
            sub.id,
        )

    async def delete_subscription(self, sub_id: int) -> None:
        await self._exec("DELETE FROM subscriptions WHERE id = ?", sub_id)

    async def live_subscriptions(self) -> list[Subscription]:
        """Включённые подписки чатов, у которых не стоит пауза."""
        rows = await self._all(
            """SELECT s.* FROM subscriptions s JOIN users u ON u.user_id = s.chat_id
               WHERE s.enabled = 1 AND u.active = 1"""
        )
        return [_sub(r) for r in rows]

    # --- каналы ---

    async def link_channel(self, channel_id: int, owner_id: int, title: str) -> None:
        await self._exec(
            """INSERT INTO channels(channel_id, owner_id, title, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET owner_id = excluded.owner_id, title = excluded.title""",
            channel_id,
            owner_id,
            title,
            int(time.time()),
        )

    async def channels_of(self, owner_id: int) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM channels WHERE owner_id = ? ORDER BY title", owner_id)

    async def channel_ids(self) -> set[int]:
        return {r[0] for r in await self._all("SELECT channel_id FROM channels")}

    async def unlink_channel(self, channel_id: int) -> list[int]:
        """Отвязывает канал; подписки, славшие туда, снова шлют владельцу. Возвращает id владельцев."""
        owners = [r[0] for r in await self._all("SELECT owner_id FROM channels WHERE channel_id = ?", channel_id)]
        await self.conn.execute("UPDATE subscriptions SET target_id = NULL WHERE target_id = ?", (channel_id,))
        await self.conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()
        return owners

    # --- коллекции и атрибуты ---

    async def upsert_collection(self, gift_id: int, title: str, slug: str, total: int | None) -> None:
        await self._exec(
            """INSERT INTO collections(gift_id, title, slug, total) VALUES (?, ?, ?, ?)
               ON CONFLICT(gift_id) DO UPDATE SET title = excluded.title, slug = excluded.slug,
               total = COALESCE(excluded.total, collections.total)""",
            gift_id,
            title,
            slug,
            total,
        )

    async def collections(self, enabled_only: bool = True) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM collections"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return await self._all(sql + " ORDER BY title")

    async def collection(self, gift_id: int):
        rows = await self._all("SELECT * FROM collections WHERE gift_id = ?", gift_id)
        return rows[0] if rows else None

    async def find_collections(self, query: str) -> list[aiosqlite.Row]:
        q = query.casefold().strip()
        rows = await self.collections()
        exact = [r for r in rows if r["title"].casefold() == q or r["slug"].casefold() == q]
        return exact or [r for r in rows if q in r["title"].casefold() or q in r["slug"].casefold()]

    async def set_last_num(self, gift_id: int, num: int) -> None:
        await self._exec("UPDATE collections SET last_num = ? WHERE gift_id = ?", num, gift_id)

    async def set_slug(self, gift_id: int, slug: str) -> None:
        await self._exec("UPDATE collections SET slug = ? WHERE gift_id = ?", slug, gift_id)

    async def update_market(self, gift_id: int, floor: int | None, resale_count: int | None, history: bool) -> None:
        await self.conn.execute(
            "UPDATE collections SET floor = ?, resale_count = ? WHERE gift_id = ?", (floor, resale_count, gift_id)
        )
        if history:
            await self.conn.execute(
                "INSERT INTO floors(gift_id, ts, floor, resale_count) VALUES (?, ?, ?, ?)",
                (gift_id, int(time.time()), floor, resale_count),
            )
        await self.conn.commit()

    async def floor_history(self, gift_id: int, since: int) -> list[tuple[int, int | None]]:
        rows = await self._all("SELECT ts, floor FROM floors WHERE gift_id = ? AND ts > ? ORDER BY ts", gift_id, since)
        return [(r[0], r[1]) for r in rows]

    async def stale_attributes(self, older_than: int) -> list[int]:
        rows = await self._all(
            "SELECT gift_id FROM collections WHERE enabled = 1 AND COALESCE(attrs_updated, 0) < ?", older_than
        )
        return [r[0] for r in rows]

    async def save_attributes(self, gift_id: int, items: list[tuple[str, int, str, int | None]]) -> None:
        await self.conn.execute("DELETE FROM attributes WHERE gift_id = ?", (gift_id,))
        await self.conn.executemany(
            "INSERT OR REPLACE INTO attributes(gift_id, kind, attr_id, name, rarity) VALUES (?, ?, ?, ?, ?)",
            [(gift_id, *item) for item in items],
        )
        await self.conn.execute(
            "UPDATE collections SET attrs_updated = ? WHERE gift_id = ?", (int(time.time()), gift_id)
        )
        await self.conn.commit()

    async def attribute_options(self, kind: str, gift_ids: list[int] | None) -> list[tuple[int, str, int | None]]:
        """Уникальные по названию атрибуты: (attr_id, name, самая низкая редкость), редкие — первыми."""
        sql = "SELECT MIN(attr_id), name, MIN(rarity) FROM attributes WHERE kind = ?"
        args: list = [kind]
        if gift_ids:
            sql += f" AND gift_id IN ({','.join('?' * len(gift_ids))})"
            args += gift_ids
        sql += " GROUP BY name ORDER BY MIN(rarity), name"
        return [(r[0], r[1], r[2]) for r in await self._all(sql, *args)]

    async def attribute_name(self, kind: str, attr_id: int) -> str | None:
        return await self._one("SELECT name FROM attributes WHERE kind = ? AND attr_id = ? LIMIT 1", kind, attr_id)

    # --- улучшения и выставления ---

    async def save_upgrade(self, u: Upgrade) -> bool:
        row = u.to_row()
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        return await self._exec(f"INSERT OR IGNORE INTO upgrades({cols}) VALUES ({marks})", *row.values()) > 0

    async def save_listing(self, u: Upgrade) -> bool:
        row = {"key": listing_key(u), **u.to_row()}
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        return await self._exec(f"INSERT OR IGNORE INTO listings({cols}) VALUES ({marks})", *row.values()) > 0

    async def known_listings(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        rows = await self._all(f"SELECT key FROM listings WHERE key IN ({','.join('?' * len(keys))})", *keys)
        return {r[0] for r in rows}

    async def recent_upgrades(self, limit: int = 500) -> list[Upgrade]:
        rows = await self._all("SELECT * FROM upgrades ORDER BY detected_at DESC, num DESC LIMIT ?", limit)
        return [_upgrade(r) for r in rows]

    async def recent_listings(self, limit: int = 500) -> list[Upgrade]:
        rows = await self._all("SELECT * FROM listings ORDER BY detected_at DESC LIMIT ?", limit)
        return [_upgrade(r) for r in rows]

    async def rarest_upgrades(self, since: int, limit: int = 10) -> list[Upgrade]:
        rows = await self._all(
            """SELECT * FROM upgrades
               WHERE detected_at > ? AND model_rarity > 0 AND backdrop_rarity > 0 AND symbol_rarity > 0
               ORDER BY model_rarity * backdrop_rarity * symbol_rarity, num LIMIT ?""",
            since,
            limit,
        )
        return [_upgrade(r) for r in rows]

    async def best_deals(self, since: int, limit: int = 10) -> list[Upgrade]:
        rows = await self._all(
            """SELECT * FROM listings WHERE detected_at > ? AND floor_stars > 0 AND resale_stars > 0
               ORDER BY CAST(resale_stars AS REAL) / floor_stars LIMIT ?""",
            since,
            limit,
        )
        return [_upgrade(r) for r in rows]

    async def top_collections(self, since: int, limit: int = 10) -> list[tuple[str, int]]:
        rows = await self._all(
            """SELECT collection, COUNT(*) FROM upgrades WHERE detected_at > ?
               GROUP BY collection ORDER BY COUNT(*) DESC LIMIT ?""",
            since,
            limit,
        )
        return [(r[0], r[1]) for r in rows]

    async def upgrades_count(self, gift_id: int, since: int) -> int:
        return await self._one("SELECT COUNT(*) FROM upgrades WHERE gift_id = ? AND detected_at > ?", gift_id, since)

    async def hourly_upgrades(self, gift_id: int, since: int) -> list[tuple[int, int]]:
        rows = await self._all(
            """SELECT detected_at / 3600 * 3600 AS hour, COUNT(*) FROM upgrades
               WHERE gift_id = ? AND detected_at > ? GROUP BY hour ORDER BY hour""",
            gift_id,
            since,
        )
        return [(r[0], r[1]) for r in rows]

    async def top_models(self, gift_id: int, since: int, limit: int = 5) -> list[tuple[str, int, int | None]]:
        rows = await self._all(
            """SELECT model, COUNT(*), MIN(model_rarity) FROM upgrades WHERE gift_id = ? AND detected_at > ?
               AND model IS NOT NULL GROUP BY model ORDER BY COUNT(*) DESC LIMIT ?""",
            gift_id,
            since,
            limit,
        )
        return [(r[0], r[1], r[2]) for r in rows]

    async def search_upgrades(self, words: list[str], limit: int = 20) -> list[Upgrade]:
        sql = "SELECT * FROM upgrades"
        args: list = []
        conds = []
        for word in words:
            if word.lstrip("#").isdigit():
                conds.append("num = ?")
                args.append(int(word.lstrip("#")))
            else:
                conds.append("(collection LIKE ? OR model LIKE ? OR backdrop LIKE ? OR symbol LIKE ? OR slug LIKE ?)")
                args += [f"%{word}%"] * 5
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY detected_at DESC LIMIT ?"
        return [_upgrade(r) for r in await self._all(sql, *args, limit)]

    # --- слежка за подарками ---

    async def add_tracked(self, chat_id: int, slug: str) -> bool:
        await self.conn.execute(
            "INSERT OR IGNORE INTO gift_states(slug, snapshot, checked_at) VALUES (?, NULL, 0)", (slug,)
        )
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO tracked(chat_id, slug, created_at) VALUES (?, ?, ?)",
            (chat_id, slug, int(time.time())),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_tracked(self, chat_id: int, slug: str) -> None:
        await self.conn.execute("DELETE FROM tracked WHERE chat_id = ? AND slug = ?", (chat_id, slug))
        await self.conn.execute("DELETE FROM gift_states WHERE slug NOT IN (SELECT slug FROM tracked)")
        await self.conn.commit()

    async def tracked_of(self, chat_id: int) -> list[str]:
        return [
            r[0] for r in await self._all("SELECT slug FROM tracked WHERE chat_id = ? ORDER BY created_at", chat_id)
        ]

    async def tracking_chats(self, slug: str) -> list[int]:
        rows = await self._all(
            """SELECT t.chat_id FROM tracked t JOIN users u ON u.user_id = t.chat_id
               WHERE t.slug = ? AND u.active = 1""",
            slug,
        )
        return [r[0] for r in rows]

    async def tracked_due(self, older_than: int, limit: int) -> list[tuple[str, dict | None]]:
        rows = await self._all(
            """SELECT slug, snapshot FROM gift_states WHERE checked_at < ?
               AND slug IN (SELECT slug FROM tracked) ORDER BY checked_at LIMIT ?""",
            older_than,
            limit,
        )
        return [(r[0], json.loads(r[1]) if r[1] else None) for r in rows]

    async def save_state(self, slug: str, snapshot: dict | None) -> None:
        await self._exec(
            "UPDATE gift_states SET snapshot = ?, checked_at = ? WHERE slug = ?",
            json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
            int(time.time()),
            slug,
        )

    # --- обслуживание ---

    async def prune(self, keep_seconds: float) -> int:
        cutoff = int(time.time() - keep_seconds)
        removed = 0
        for table, column in (("upgrades", "detected_at"), ("listings", "detected_at"), ("floors", "ts")):
            cur = await self.conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
            removed += cur.rowcount
        await self.conn.commit()
        return removed

    async def stats(self) -> dict:
        now = int(time.time())
        return {
            "users": await self._one("SELECT COUNT(*) FROM users"),
            "active": await self._one("SELECT COUNT(*) FROM users WHERE active = 1"),
            "premium": await self._one("SELECT COUNT(*) FROM users WHERE premium_until > ?", now),
            "subscriptions": await self._one("SELECT COUNT(*) FROM subscriptions"),
            "channels": await self._one("SELECT COUNT(*) FROM channels"),
            "tracked": await self._one("SELECT COUNT(*) FROM tracked"),
            "collections": await self._one("SELECT COUNT(*) FROM collections WHERE enabled = 1"),
            "upgrades": await self._one("SELECT COUNT(*) FROM upgrades"),
            "upgrades_1h": await self._one("SELECT COUNT(*) FROM upgrades WHERE detected_at > ?", now - 3600),
            "upgrades_24h": await self._one("SELECT COUNT(*) FROM upgrades WHERE detected_at > ?", now - 86400),
            "listings_24h": await self._one("SELECT COUNT(*) FROM listings WHERE detected_at > ?", now - 86400),
        }
