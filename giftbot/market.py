"""Маркет: флоры коллекций и лента новых выставлений на продажу.

Один запрос payments.getStarGifts раз в MARKET_REFRESH секунд даёт по всем
коллекциям сразу минимальную цену (флор) и число лотов. Если у коллекции что-то
поменялось, скоро запрашиваем её ленту продаж (новые лоты сверху) и находим новые
выставления. Ленты опрашиваются только для коллекций, которые нужны чьим-то
подпискам на продажи, так что лишней нагрузки нет.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from telethon import errors
from telethon.tl import functions, types

from .config import config
from .db import Database, listing_key
from .models import Upgrade
from .parsing import entities_of, parse_gift
from .pool import FATAL_ERRORS, PoolExhausted

log = logging.getLogger(__name__)

PAGE_SIZE = 30
# Точка истории флора пишется не чаще, чем раз в столько секунд на коллекцию.
HISTORY_EVERY = 600
NEEDED_CACHE = 30

OnListing = Callable[[Upgrade], Awaitable[None]]


class Market:
    def __init__(self, call, db: Database, on_listing: OnListing):
        self.call = call
        self.db = db
        self.on_listing = on_listing
        self.floors: dict[int, int | None] = {}
        self.counts: dict[int, int | None] = {}
        self.ref_floor: dict[int, int | None] = {}  # флор до последнего изменения — с ним сравниваем цену
        self.seen: dict[int, set[str]] = {}
        self.due: dict[int, float] = {}
        self.titles: dict[int, str] = {}
        self.found = 0
        self._catalog_at = float("-inf")
        self._history_at: dict[int, float] = {}
        self._needed: tuple[float, set[int] | None] = (float("-inf"), set())

    def floor_of(self, gift_id: int) -> int | None:
        return self.floors.get(gift_id)

    async def refresh_catalog(self) -> None:
        resp = await self.call(functions.payments.GetStarGiftsRequest(hash=0))
        now = time.monotonic()
        self.titles = {r["gift_id"]: r["title"] for r in await self.db.collections()}
        for gift in resp.gifts:
            if not isinstance(gift, types.StarGift) or gift.id not in self.titles:
                continue
            floor, count = gift.resell_min_stars, gift.availability_resale
            old = (self.floors.get(gift.id), self.counts.get(gift.id))
            changed = old != (floor, count)
            if changed and gift.id in self.floors:
                self.ref_floor[gift.id] = old[0] or floor
                self.due[gift.id] = 0  # на маркете что-то произошло — смотрим ленту сейчас
            self.floors[gift.id], self.counts[gift.id] = floor, count
            history = now - self._history_at.get(gift.id, float("-inf")) > HISTORY_EVERY
            if changed or history:
                await self.db.update_market(gift.id, floor, count, history)
                if history:
                    self._history_at[gift.id] = now
        self._catalog_at = now

    async def needed(self) -> set[int]:
        """Коллекции, ленты которых нужны подпискам на выставления."""
        at, cached = self._needed
        if time.monotonic() - at < NEEDED_CACHE:
            return cached
        wanted_all, titles = False, set()
        for sub in await self.db.live_subscriptions():
            if "listing" not in sub.filters.events:
                continue
            if not sub.filters.collections:
                wanted_all = True
                break
            titles.update(sub.filters.collections)
        ids = {gid for gid, title in self.titles.items() if wanted_all or title in titles}
        self._needed = (time.monotonic(), ids)
        return ids

    async def poll(self, gift_id: int) -> int:
        """Смотрит ленту продаж коллекции. Возвращает число новых выставлений."""
        title = self.titles.get(gift_id, str(gift_id))
        try:
            resp = await self.call(
                functions.payments.GetResaleStarGiftsRequest(gift_id=gift_id, offset="", limit=PAGE_SIZE)
            )
        except (*FATAL_ERRORS, PoolExhausted):
            raise
        except errors.RPCError as e:
            log.debug("Лента продаж %s недоступна: %s", title, e)
            return 0
        entities = entities_of(resp)
        reference = self.ref_floor.get(gift_id) or self.floors.get(gift_id)
        listings = []
        for gift in resp.gifts:
            if isinstance(gift, types.StarGiftUnique):
                u = parse_gift(gift, entities, title, event="listing")
                if u.resale_stars or u.resale_ton:
                    u.floor_stars = reference
                    listings.append(u)
        keys = {listing_key(u) for u in listings}
        first_look = gift_id not in self.seen
        previous = self.seen.get(gift_id, set())
        self.seen[gift_id] = keys
        self.ref_floor[gift_id] = self.floors.get(gift_id)
        if first_look:
            return 0  # то, что уже лежало на маркете до запуска, не рассылаем
        fresh = [u for u in listings if listing_key(u) not in previous]
        known = await self.db.known_listings([listing_key(u) for u in fresh])
        count = 0
        for u in fresh:
            if listing_key(u) in known or not await self.db.save_listing(u):
                continue
            count += 1
            self.found += 1
            try:
                await self.on_listing(u)
            except Exception:
                log.exception("Ошибка обработки выставления %s", u.slug)
        return count

    async def run(self) -> None:
        while True:
            try:
                now = time.monotonic()
                if now - self._catalog_at > config.market_refresh:
                    await self.refresh_catalog()
                needed = await self.needed()
                due = sorted((g for g in needed if self.due.get(g, 0) <= now), key=lambda g: self.due.get(g, 0))
                for gift_id in due[:5]:
                    self.due[gift_id] = time.monotonic() + config.market_poll_max
                    await self.poll(gift_id)
                if not due:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except (*FATAL_ERRORS, PoolExhausted):
                return
            except Exception:
                log.exception("Ошибка в цикле маркета")
                await asyncio.sleep(10)
