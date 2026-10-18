"""Слежка за конкретными подарками: смена владельца, выставление, снятие, цена, сжигание."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .config import config
from .db import Database
from .models import Upgrade
from .parsing import parse_unique
from .pool import FATAL_ERRORS, PoolExhausted
from .render import track_changes_text
from .watcher import fetch_unique

log = logging.getLogger(__name__)

Notify = Callable[[int, str, str], Awaitable[None]]  # (chat_id, текст, slug)

SNAPSHOT_FIELDS = ("owner_id", "owner_username", "owner_name", "owner_address", "resale_stars", "resale_ton")


def snapshot(u: Upgrade | None, burned: bool = False) -> dict:
    if u is None:
        return {"missing": True}
    data = {f: getattr(u, f) for f in SNAPSHOT_FIELDS}
    data.update(collection=u.collection, num=u.num, burned=burned)
    return data


def owner_key(s: dict) -> str | None:
    return str(s.get("owner_id") or s.get("owner_address") or s.get("owner_name") or "") or None


def diff(old: dict, new: dict) -> list[tuple[str, dict]]:
    """Список изменений: (вид, детали)."""
    if new.get("missing"):
        return [] if old.get("missing") else [("missing", {})]
    if old.get("missing"):
        return [("back", {})]
    changes = []
    if new.get("burned") and not old.get("burned"):
        changes.append(("burned", {}))
    old_price = (old.get("resale_stars"), old.get("resale_ton"))
    new_price = (new.get("resale_stars"), new.get("resale_ton"))
    listed_before, listed_now = any(old_price), any(new_price)
    if owner_key(old) != owner_key(new):
        kind = "sold" if listed_before and not listed_now else "owner"
        changes.append((kind, {"old": old, "price": old_price}))
        if listed_now:
            changes.append(("listed", {"price": new_price}))
    elif listed_now and not listed_before:
        changes.append(("listed", {"price": new_price}))
    elif listed_before and not listed_now:
        changes.append(("unlisted", {}))
    elif old_price != new_price:
        changes.append(("price", {"old": old_price, "price": new_price}))
    return changes


class Tracker:
    def __init__(self, call, db: Database, notify: Notify):
        self.call = call
        self.db = db
        self.notify = notify
        self.checked = 0

    async def check(self, slug: str, old: dict | None) -> list[tuple[str, dict]]:
        resp = await fetch_unique(self.call, slug)
        u = parse_unique(resp, resp.gift.title) if resp else None
        new = snapshot(u, bool(resp and resp.gift.burned))
        self.checked += 1
        await self.db.save_state(slug, new)
        if old is None:
            return []  # первый снимок — запоминаем молча
        changes = diff(old, new)
        if changes:
            text = track_changes_text(slug, u, new, changes)
            for chat_id in await self.db.tracking_chats(slug):
                await self.notify(chat_id, text, slug)
        return changes

    async def run(self) -> None:
        while True:
            try:
                due = await self.db.tracked_due(int(time.time() - config.track_interval), 10)
                for slug, old in due:
                    await self.check(slug, old)
                await asyncio.sleep(5 if not due else 0.5)
            except asyncio.CancelledError:
                raise
            except (*FATAL_ERRORS, PoolExhausted):
                return
            except Exception:
                log.exception("Ошибка слежки за подарками")
                await asyncio.sleep(10)
