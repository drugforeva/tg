import json
import time

import aiosqlite

from .models import Filters, Subscription, Upgrade

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,  -- id чата: личка или группа
    active INTEGER NOT NULL DEFAULT 1,
    filters TEXT NOT NULL DEFAULT '{}',  -- устарело, перенесено в subscriptions
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    filters TEXT NOT NULL DEFAULT '{}',
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
"""

# Колонки, добавленные после первой версии: (таблица, колонка, объявление)
MIGRATIONS = [
    ("users", "digest_window", "INTEGER NOT NULL DEFAULT 0"),
    ("collections", "attrs_updated", "INTEGER"),
    ("upgrades", "sender_id", "INTEGER"),
    ("upgrades", "sender_name", "TEXT"),
    ("upgrades", "sender_username", "TEXT"),
    ("upgrades", "resale_ton", "INTEGER"),
    ("upgrades", "value_usd", "INTEGER"),
    ("upgrades", "crafted", "INTEGER NOT NULL DEFAULT 0"),
]


def _sub(row) -> Subscription:
    return Subscription(
        id=row["id"],
        chat_id=row["chat_id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        filters=Filters.from_dict(json.loads(row["filters"])),
    )


def _upgrade(row) -> Upgrade:
    data = dict(row)
    data["crafted"] = bool(data.get("crafted"))
    return Upgrade(**{k: v for k, v in data.items() if k in Upgrade.__dataclass_fields__})


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
               WHERE user_id NOT IN (SELECT chat_id FROM subscriptions)"""
        )
        for row in await cur.fetchall():
            await self.conn.execute(
                "INSERT INTO subscriptions(chat_id, name, filters, created_at) VALUES (?, ?, ?, ?)",
                (row["user_id"], "Основная", row["filters"], row["created_at"]),
            )

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, *args):
        cur = await self.conn.execute(sql, args)
        row = await cur.fetchone()
        return row[0] if row else None

    # --- чаты ---

    async def ensure_user(self, chat_id: int) -> bool:
        """Создаёт чат при первом обращении. Возвращает True, если он новый."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)",
            (chat_id, int(time.time())),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_active(self, chat_id: int, active: bool) -> None:
        await self.conn.execute("UPDATE users SET active = ? WHERE user_id = ?", (int(active), chat_id))
        await self.conn.commit()

    async def is_active(self, chat_id: int) -> bool:
        return bool(await self._one("SELECT active FROM users WHERE user_id = ?", chat_id))

    async def get_window(self, chat_id: int) -> int:
        return await self._one("SELECT digest_window FROM users WHERE user_id = ?", chat_id) or 0

    async def set_window(self, chat_id: int, seconds: int) -> None:
        await self.conn.execute("UPDATE users SET digest_window = ? WHERE user_id = ?", (seconds, chat_id))
        await self.conn.commit()

    # --- подписки ---

    async def subscriptions(self, chat_id: int) -> list[Subscription]:
        cur = await self.conn.execute("SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY id", (chat_id,))
        return [_sub(r) for r in await cur.fetchall()]

    async def get_subscription(self, sub_id: int) -> Subscription | None:
        cur = await self.conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
        row = await cur.fetchone()
        return _sub(row) if row else None

    async def create_subscription(self, chat_id: int, name: str, f: Filters | None = None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO subscriptions(chat_id, name, filters, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, name, json.dumps((f or Filters()).to_dict(), ensure_ascii=False), int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def save_subscription(self, sub: Subscription) -> None:
        await self.conn.execute(
            "UPDATE subscriptions SET name = ?, enabled = ?, filters = ? WHERE id = ?",
            (sub.name, int(sub.enabled), json.dumps(sub.filters.to_dict(), ensure_ascii=False), sub.id),
        )
        await self.conn.commit()

    async def delete_subscription(self, sub_id: int) -> None:
        await self.conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        await self.conn.commit()

    async def live_subscriptions(self) -> list[Subscription]:
        """Включённые подписки чатов, у которых не стоит пауза."""
        cur = await self.conn.execute(
            """SELECT s.* FROM subscriptions s JOIN users u ON u.user_id = s.chat_id
               WHERE s.enabled = 1 AND u.active = 1"""
        )
        return [_sub(r) for r in await cur.fetchall()]

    # --- коллекции и атрибуты ---

    async def upsert_collection(self, gift_id: int, title: str, slug: str, total: int | None) -> None:
        await self.conn.execute(
            """INSERT INTO collections(gift_id, title, slug, total) VALUES (?, ?, ?, ?)
               ON CONFLICT(gift_id) DO UPDATE SET title = excluded.title, slug = excluded.slug,
               total = COALESCE(excluded.total, collections.total)""",
            (gift_id, title, slug, total),
        )
        await self.conn.commit()

    async def collections(self, enabled_only: bool = True) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM collections"
        if enabled_only:
            sql += " WHERE enabled = 1"
        cur = await self.conn.execute(sql + " ORDER BY title")
        return list(await cur.fetchall())

    async def set_last_num(self, gift_id: int, num: int) -> None:
        await self.conn.execute("UPDATE collections SET last_num = ? WHERE gift_id = ?", (num, gift_id))
        await self.conn.commit()

    async def set_slug(self, gift_id: int, slug: str) -> None:
        await self.conn.execute("UPDATE collections SET slug = ? WHERE gift_id = ?", (slug, gift_id))
        await self.conn.commit()

    async def stale_attributes(self, older_than: int) -> list[int]:
        cur = await self.conn.execute(
            "SELECT gift_id FROM collections WHERE enabled = 1 AND COALESCE(attrs_updated, 0) < ?",
            (older_than,),
        )
        return [r["gift_id"] for r in await cur.fetchall()]

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
        cur = await self.conn.execute(sql, args)
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]

    async def attribute_name(self, kind: str, attr_id: int) -> str | None:
        return await self._one("SELECT name FROM attributes WHERE kind = ? AND attr_id = ? LIMIT 1", kind, attr_id)

    # --- улучшения ---

    async def save_upgrade(self, u: Upgrade) -> bool:
        row = u.to_row()
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = await self.conn.execute(f"INSERT OR IGNORE INTO upgrades({cols}) VALUES ({marks})", tuple(row.values()))
        await self.conn.commit()
        return cur.rowcount > 0

    async def recent_upgrades(self, limit: int = 500) -> list[Upgrade]:
        cur = await self.conn.execute("SELECT * FROM upgrades ORDER BY detected_at DESC, num DESC LIMIT ?", (limit,))
        return [_upgrade(r) for r in await cur.fetchall()]

    async def rarest_upgrades(self, since: int, limit: int = 10) -> list[Upgrade]:
        cur = await self.conn.execute(
            """SELECT * FROM upgrades
               WHERE detected_at > ? AND model_rarity > 0 AND backdrop_rarity > 0 AND symbol_rarity > 0
               ORDER BY model_rarity * backdrop_rarity * symbol_rarity, num LIMIT ?""",
            (since, limit),
        )
        return [_upgrade(r) for r in await cur.fetchall()]

    async def top_collections(self, since: int, limit: int = 10) -> list[tuple[str, int]]:
        cur = await self.conn.execute(
            """SELECT collection, COUNT(*) FROM upgrades WHERE detected_at > ?
               GROUP BY collection ORDER BY COUNT(*) DESC LIMIT ?""",
            (since, limit),
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]

    async def prune(self, keep_seconds: float) -> int:
        cur = await self.conn.execute("DELETE FROM upgrades WHERE detected_at < ?", (int(time.time() - keep_seconds),))
        await self.conn.commit()
        return cur.rowcount

    async def stats(self) -> dict:
        now = int(time.time())
        return {
            "users": await self._one("SELECT COUNT(*) FROM users"),
            "active": await self._one("SELECT COUNT(*) FROM users WHERE active = 1"),
            "subscriptions": await self._one("SELECT COUNT(*) FROM subscriptions"),
            "collections": await self._one("SELECT COUNT(*) FROM collections WHERE enabled = 1"),
            "upgrades": await self._one("SELECT COUNT(*) FROM upgrades"),
            "upgrades_1h": await self._one("SELECT COUNT(*) FROM upgrades WHERE detected_at > ?", now - 3600),
            "upgrades_24h": await self._one("SELECT COUNT(*) FROM upgrades WHERE detected_at > ?", now - 86400),
        }
