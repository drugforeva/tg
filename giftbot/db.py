import json
import time

import aiosqlite

from .models import Filters, Upgrade

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    filters TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS collections (
    gift_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    last_num INTEGER,
    total INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1
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


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # --- пользователи ---

    async def ensure_user(self, user_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)",
            (user_id, int(time.time())),
        )
        await self.conn.commit()

    async def get_filters(self, user_id: int) -> Filters:
        cur = await self.conn.execute("SELECT filters FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return Filters.from_dict(json.loads(row["filters"])) if row else Filters()

    async def set_filters(self, user_id: int, f: Filters) -> None:
        await self.conn.execute(
            "UPDATE users SET filters = ? WHERE user_id = ?",
            (json.dumps(f.to_dict(), ensure_ascii=False), user_id),
        )
        await self.conn.commit()

    async def set_active(self, user_id: int, active: bool) -> None:
        await self.conn.execute(
            "UPDATE users SET active = ? WHERE user_id = ?", (int(active), user_id)
        )
        await self.conn.commit()

    async def is_active(self, user_id: int) -> bool:
        cur = await self.conn.execute("SELECT active FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row["active"])

    async def active_subscribers(self) -> list[tuple[int, Filters]]:
        cur = await self.conn.execute("SELECT user_id, filters FROM users WHERE active = 1")
        return [
            (r["user_id"], Filters.from_dict(json.loads(r["filters"])))
            for r in await cur.fetchall()
        ]

    # --- коллекции ---

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

    async def collection_titles(self) -> list[str]:
        return [r["title"] for r in await self.collections()]

    async def set_last_num(self, gift_id: int, num: int) -> None:
        await self.conn.execute(
            "UPDATE collections SET last_num = ? WHERE gift_id = ?", (num, gift_id)
        )
        await self.conn.commit()

    async def disable_collection(self, gift_id: int) -> None:
        await self.conn.execute("UPDATE collections SET enabled = 0 WHERE gift_id = ?", (gift_id,))
        await self.conn.commit()

    # --- улучшения ---

    async def save_upgrade(self, u: Upgrade) -> bool:
        row = u.to_row()
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = await self.conn.execute(
            f"INSERT OR IGNORE INTO upgrades({cols}) VALUES ({marks})", tuple(row.values())
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def recent_upgrades(self, limit: int = 500) -> list[Upgrade]:
        cur = await self.conn.execute(
            "SELECT * FROM upgrades ORDER BY detected_at DESC, num DESC LIMIT ?", (limit,)
        )
        return [Upgrade(**dict(r)) for r in await cur.fetchall()]

    async def stats(self) -> dict:
        async def one(sql: str, *args) -> int:
            cur = await self.conn.execute(sql, args)
            return (await cur.fetchone())[0]

        day_ago = int(time.time()) - 86400
        return {
            "users": await one("SELECT COUNT(*) FROM users"),
            "active": await one("SELECT COUNT(*) FROM users WHERE active = 1"),
            "collections": await one("SELECT COUNT(*) FROM collections WHERE enabled = 1"),
            "upgrades": await one("SELECT COUNT(*) FROM upgrades"),
            "upgrades_24h": await one("SELECT COUNT(*) FROM upgrades WHERE detected_at > ?", day_ago),
        }
