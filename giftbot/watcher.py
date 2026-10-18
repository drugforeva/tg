"""Наблюдатель: находит только что улучшенные подарки.

Как это работает. Улучшенные подарки нумеруются по порядку внутри коллекции
(t.me/nft/PlushPepe-1, PlushPepe-2, ...), а каждый из них сообщает, сколько
экземпляров коллекции уже выпущено (availability_issued). Мы запрашиваем
последний известный номер, узнаём текущее issued и докачиваем все номера
между ними — это и есть новые улучшения.

Коллекции опрашиваются адаптивно: где только что были улучшения — каждые
POLL_MIN секунд, тихие — всё реже, до POLL_MAX. Запросы идут через пул
аккаунтов (pool.py), который сам распределяет нагрузку и переживает FloodWait.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from telethon import errors
from telethon.tl import functions, types

from .config import config
from .db import Database
from .models import Upgrade
from .parsing import parse_attributes, parse_unique, title_to_slug
from .pool import FATAL_ERRORS, PoolExhausted

log = logging.getLogger(__name__)

# Сколько новых номеров одной коллекции докачивать за один заход.
MAX_FETCH_PER_PASS = 40
# После скольких неудачных попыток считать номер пропавшим и идти дальше.
MAX_MISSES = 3
# Как часто обновлять списки моделей/фонов/узоров коллекций.
ATTRIBUTES_TTL = 24 * 3600
# Как часто пробовать найти настоящий слаг коллекции через маркет.
DISCOVERY_RETRY = 3600

OnUpgrade = Callable[[Upgrade], Awaitable[None]]
OnAlert = Callable[[str], Awaitable[None]]
Call = Callable[[object], Awaitable[object]]


@dataclass
class CollectionState:
    gift_id: int
    title: str
    slug: str
    last_num: int | None
    due: float = 0.0
    interval: float = 0.0
    misses: dict[int, int] = field(default_factory=dict)
    discovery_at: float = float("-inf")
    warned: bool = False


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.time)
    found: int = 0
    last_found_at: float | None = None
    last_error: str | None = None
    fatal: str | None = None


async def fetch_unique(call: Call, slug: str) -> types.payments.UniqueStarGift | None:
    """Уникальный подарок по слагу или None, если такого (пока) нет."""
    try:
        resp = await call(functions.payments.GetUniqueStarGiftRequest(slug=slug))
    except (*FATAL_ERRORS, PoolExhausted):
        raise
    except errors.RPCError as e:
        if "SLUG_INVALID" not in str(e) and "NOT_FOUND" not in str(e):
            log.warning("Ошибка при запросе %s: %s", slug, e)
        return None
    return resp if isinstance(resp.gift, types.StarGiftUnique) else None


class Watcher:
    def __init__(
        self,
        call: Call,
        db: Database,
        on_upgrade: OnUpgrade,
        on_alert: OnAlert | None = None,
        floor_of: Callable[[int], int | None] | None = None,
    ):
        self.call = call  # ClientPool или любой вызываемый клиент Telethon
        self.db = db
        self.on_upgrade = on_upgrade
        self.on_alert = on_alert
        self.floor_of = floor_of  # флор коллекции с маркета, чтобы показать скидку
        self.metrics = Metrics()
        self.states: dict[int, CollectionState] = {}
        self._collections_updated = float("-inf")

    async def fetch(self, slug: str) -> types.payments.UniqueStarGift | None:
        return await fetch_unique(self.call, slug)

    # ---------- коллекции ----------

    async def load_states(self) -> None:
        for row in await self.db.collections():
            self.states.setdefault(
                row["gift_id"],
                CollectionState(row["gift_id"], row["title"], row["slug"], row["last_num"]),
            )

    async def refresh_collections(self) -> None:
        resp = await self.call(functions.payments.GetStarGiftsRequest(hash=0))
        known = {r["gift_id"]: r for r in await self.db.collections(enabled_only=False)}
        for gift in resp.gifts:
            if not isinstance(gift, types.StarGift) or gift.upgrade_stars is None:
                continue
            row = known.get(gift.id)
            title = gift.title or (row["title"] if row else f"Gift {gift.id}")
            if title in config.slug_overrides:
                slug = config.slug_overrides[title]
            elif row:
                slug = row["slug"]  # уже проверенный или найденный через маркет
            else:
                slug = title_to_slug(title)
            await self.db.upsert_collection(gift.id, title, slug, gift.availability_total)
            state = self.states.get(gift.id)
            if state:
                state.title, state.slug = title, slug
            else:
                self.states[gift.id] = CollectionState(gift.id, title, slug, row["last_num"] if row else None)
        self._collections_updated = time.monotonic()
        log.info("Коллекций с улучшением: %d", len(self.states))

    async def discover_slug(self, state: CollectionState) -> bool:
        """Узнаёт настоящий слаг по любому подарку коллекции, выставленному на маркете."""
        state.discovery_at = time.monotonic()
        try:
            resp = await self.call(
                functions.payments.GetResaleStarGiftsRequest(gift_id=state.gift_id, offset="", limit=1)
            )
        except (*FATAL_ERRORS, PoolExhausted):
            raise
        except errors.RPCError as e:
            log.debug("Маркет для %s недоступен: %s", state.title, e)
            return False
        for gift in resp.gifts:
            if isinstance(gift, types.StarGiftUnique) and "-" in gift.slug:
                slug = gift.slug.rsplit("-", 1)[0]
                if slug != state.slug:
                    log.info("%s: слаг исправлен %s -> %s", state.title, state.slug, slug)
                    state.slug = slug
                    await self.db.set_slug(state.gift_id, slug)
                    return True
        return False

    async def check_collection(self, state: CollectionState) -> tuple[int, bool]:
        """Проверяет коллекцию. Возвращает (сколько новых найдено, есть ли ещё недокачанные)."""
        last_num = state.last_num
        anchor = await self.fetch(f"{state.slug}-{max(last_num or 1, 1)}")
        if anchor is None and last_num:
            anchor = await self.fetch(f"{state.slug}-1")
        if anchor is None and time.monotonic() - state.discovery_at > DISCOVERY_RETRY:
            if await self.discover_slug(state):
                anchor = await self.fetch(f"{state.slug}-{max(last_num or 1, 1)}")
        if anchor is None:
            if not state.warned:
                state.warned = True
                log.info("%s: улучшенных экземпляров пока нет или неверный слаг '%s'", state.title, state.slug)
            if last_num is None:
                state.last_num = 0
                await self.db.set_last_num(state.gift_id, 0)
            return 0, False

        issued = anchor.gift.availability_issued
        if last_num is None:
            # Первый запуск: историю не рассылаем, просто запоминаем позицию.
            state.last_num = issued
            await self.db.set_last_num(state.gift_id, issued)
            log.info("%s: стартуем с номера %d", state.title, issued)
            return 0, False
        if issued <= last_num:
            return 0, False

        # Большой отрыв догоняем порциями за несколько заходов, ничего не пропуская.
        end = min(issued, last_num + MAX_FETCH_PER_PASS)
        found = 0
        for num in range(last_num + 1, end + 1):
            resp = await self.fetch(f"{state.slug}-{num}")
            if resp is None:
                state.misses[num] = state.misses.get(num, 0) + 1
                if state.misses[num] < MAX_MISSES:
                    break  # номер ещё не доступен — попробуем чуть позже
                log.info("%s #%d недоступен, пропускаем", state.title, num)
            else:
                upgrade = parse_unique(resp, state.title)
                if self.floor_of:
                    upgrade.floor_stars = self.floor_of(state.gift_id)
                if await self.db.save_upgrade(upgrade):
                    found += 1
                    self.metrics.found += 1
                    self.metrics.last_found_at = time.time()
                    try:
                        await self.on_upgrade(upgrade)
                    except Exception:
                        log.exception("Ошибка обработки %s", upgrade.slug)
            state.misses.pop(num, None)
            state.last_num = num
            await self.db.set_last_num(state.gift_id, num)
        progressed = state.last_num > last_num
        return found, progressed and state.last_num < issued

    def _reschedule(self, state: CollectionState, found: int, behind: bool) -> None:
        if behind:
            state.interval = 0
        elif found or state.misses:
            state.interval = config.poll_min
        else:
            state.interval = min(max(state.interval, config.poll_min) * 1.5, config.poll_max)
        state.due = time.monotonic() + state.interval

    # ---------- атрибуты ----------

    async def refresh_attributes(self) -> None:
        """Фоном подгружает списки моделей/фонов/узоров для выбора в фильтрах."""
        while True:
            try:
                for gift_id in await self.db.stale_attributes(int(time.time()) - ATTRIBUTES_TTL):
                    try:
                        request = functions.payments.GetStarGiftUpgradeAttributesRequest(gift_id=gift_id)
                        resp = await self.call(request)
                    except (*FATAL_ERRORS, PoolExhausted):
                        raise
                    except errors.RPCError as e:
                        # Одна проблемная коллекция не должна мешать остальным.
                        log.debug("Атрибуты %s недоступны: %s", gift_id, e)
                        continue
                    await self.db.save_attributes(gift_id, parse_attributes(resp.attributes))
                    await asyncio.sleep(2)  # не мешаем основному опросу
            except asyncio.CancelledError:
                raise
            except (*FATAL_ERRORS, PoolExhausted):
                return
            except Exception as e:
                log.warning("Не удалось обновить атрибуты: %s", e)
            await asyncio.sleep(600)

    # ---------- главный цикл ----------

    async def run(self) -> None:
        await self.load_states()
        attributes_task: asyncio.Task | None = None
        try:
            while True:
                try:
                    if time.monotonic() - self._collections_updated > config.collections_refresh:
                        await self.refresh_collections()
                        if attributes_task is None:  # стартуем, когда список коллекций уже есть
                            attributes_task = asyncio.create_task(self.refresh_attributes())
                    if not self.states:
                        await asyncio.sleep(5)
                        continue
                    state = min(self.states.values(), key=lambda s: s.due)
                    delay = state.due - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(min(delay, 1))
                        continue
                    # Если проверка упадёт, коллекция не заблокирует очередь.
                    state.due = time.monotonic() + config.poll_max
                    found, behind = await self.check_collection(state)
                    self._reschedule(state, found, behind)
                except asyncio.CancelledError:
                    raise
                except (*FATAL_ERRORS, PoolExhausted) as e:
                    self.metrics.fatal = type(e).__name__
                    log.error("Наблюдатель остановлен: %s", e)
                    if self.on_alert:
                        await self.on_alert(
                            f"⛔️ Наблюдатель остановлен: {type(e).__name__}. Улучшения больше не отслеживаются.\n"
                            "Создайте новую сессию (python -m giftbot.login) и обновите SESSION_STRING."
                        )
                    return
                except Exception as e:
                    self.metrics.last_error = f"{type(e).__name__}: {e}"
                    log.exception("Ошибка в цикле наблюдателя")
                    await asyncio.sleep(5)
        finally:
            if attributes_task:
                attributes_task.cancel()
