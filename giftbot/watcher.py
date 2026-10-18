"""Наблюдатель: через аккаунт Telegram (MTProto) находит только что улучшенные подарки.

Как это работает. Улучшенные подарки нумеруются по порядку внутри коллекции
(t.me/nft/PlushPepe-1, PlushPepe-2, ...), а каждый из них сообщает, сколько
экземпляров коллекции уже выпущено (availability_issued). Мы запрашиваем
последний известный номер, узнаём текущее issued и докачиваем все номера
между ними — это и есть новые улучшения.

Коллекции опрашиваются адаптивно: где только что были улучшения — каждые
POLL_MIN секунд, тихие — всё реже, до POLL_MAX. Все запросы идут через общий
ограничитель скорости, который сам притормаживает после FloodWait.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from telethon import TelegramClient, errors
from telethon.tl import functions, types

from .config import config
from .db import Database
from .models import Upgrade

log = logging.getLogger(__name__)

# Подарки после крафта получают уровень редкости без точного процента —
# переводим уровень в примерные промилле, чтобы работали фильтры по редкости.
TIER_PERMILLE = {
    types.StarGiftAttributeRarityLegendary: 5,
    types.StarGiftAttributeRarityEpic: 15,
    types.StarGiftAttributeRarityRare: 30,
    types.StarGiftAttributeRarityUncommon: 100,
}

# Сколько новых номеров одной коллекции докачивать за один заход.
MAX_FETCH_PER_PASS = 40
# После скольких неудачных попыток считать номер пропавшим и идти дальше.
MAX_MISSES = 3
# Как часто обновлять списки моделей/фонов/узоров коллекций.
ATTRIBUTES_TTL = 24 * 3600
# Как часто пробовать найти настоящий слаг коллекции через маркет.
DISCOVERY_RETRY = 3600

# Ошибки, после которых аккаунт-наблюдатель работать не может.
FATAL_ERRORS = (
    errors.AuthKeyUnregisteredError,
    errors.AuthKeyDuplicatedError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
)

OnUpgrade = Callable[[Upgrade], Awaitable[None]]
OnAlert = Callable[[str], Awaitable[None]]


def title_to_slug(title: str) -> str:
    """'Plush Pepe' -> 'PlushPepe', "Durov's Cap" -> 'DurovsCap', 'Jack-in-the-Box' -> 'JackInTheBox'."""
    cleaned = re.sub(r"['’`]", "", title)
    words = re.split(r"[^0-9A-Za-z]+", cleaned)
    return "".join(w[:1].upper() + w[1:] for w in words if w)


def _rarity(attr) -> int | None:
    permille = getattr(attr, "rarity_permille", None)
    if permille is not None:
        return permille
    rarity = getattr(attr, "rarity", None)
    if isinstance(rarity, types.StarGiftAttributeRarity):
        return rarity.permille
    return TIER_PERMILLE.get(type(rarity))


def _peer_id(peer) -> int | None:
    if isinstance(peer, types.PeerUser):
        return peer.user_id
    if isinstance(peer, types.PeerChannel):
        return peer.channel_id
    if isinstance(peer, types.PeerChat):
        return peer.chat_id
    return None


def _username(entity) -> str | None:
    if getattr(entity, "username", None):
        return entity.username
    for u in getattr(entity, "usernames", None) or []:
        if u.active:
            return u.username
    return None


def _display_name(entity) -> str | None:
    if isinstance(entity, types.User):
        return " ".join(x for x in (entity.first_name, entity.last_name) if x) or None
    return getattr(entity, "title", None)


def parse_attributes(attributes) -> list[tuple[str, int, str, int | None]]:
    """Атрибуты коллекции -> [(kind, attr_id, name, rarity)]."""
    items = []
    for attr in attributes:
        if isinstance(attr, types.StarGiftAttributeModel):
            items.append(("model", attr.document.id, attr.name, _rarity(attr)))
        elif isinstance(attr, types.StarGiftAttributeBackdrop):
            items.append(("backdrop", attr.backdrop_id, attr.name, _rarity(attr)))
        elif isinstance(attr, types.StarGiftAttributePattern):
            items.append(("symbol", attr.document.id, attr.name, _rarity(attr)))
    return items


def parse_unique(resp: types.payments.UniqueStarGift, collection: str) -> Upgrade:
    gift: types.StarGiftUnique = resp.gift
    u = Upgrade(
        slug=gift.slug,
        collection=collection,
        gift_id=gift.gift_id,
        num=gift.num,
        issued=gift.availability_issued,
        total=gift.availability_total,
        owner_name=gift.owner_name,
        owner_address=gift.owner_address,
        value_usd=gift.value_usd_amount,
        crafted=bool(gift.crafted),
        detected_at=int(time.time()),
    )
    entities = {e.id: e for e in [*resp.users, *resp.chats]}
    sender_peer = None
    for attr in gift.attributes:
        if isinstance(attr, types.StarGiftAttributeModel):
            u.model, u.model_rarity = attr.name, _rarity(attr)
        elif isinstance(attr, types.StarGiftAttributeBackdrop):
            u.backdrop, u.backdrop_rarity = attr.name, _rarity(attr)
        elif isinstance(attr, types.StarGiftAttributePattern):
            u.symbol, u.symbol_rarity = attr.name, _rarity(attr)
        elif isinstance(attr, types.StarGiftAttributeOriginalDetails):
            sender_peer = attr.sender_id

    if (owner_id := _peer_id(gift.owner_id)) is not None:
        u.owner_id = owner_id
        if owner := entities.get(owner_id):
            u.owner_username = _username(owner)
            u.owner_name = _display_name(owner) or u.owner_name

    if (sender_id := _peer_id(sender_peer)) is not None and sender_id != u.owner_id:
        u.sender_id = sender_id
        if sender := entities.get(sender_id):
            u.sender_username = _username(sender)
            u.sender_name = _display_name(sender)

    for amount in gift.resell_amount or []:
        if isinstance(amount, types.StarsTonAmount):
            u.resale_ton = amount.amount
        elif isinstance(amount, types.StarsAmount) and not gift.resale_ton_only:
            u.resale_stars = amount.amount
    return u


class RateLimiter:
    """Равномерно распределяет запросы; после FloodWait снижает скорость и потом плавно возвращает."""

    def __init__(self, rate: float):
        self.base = self.rate = rate
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._ok_streak = 0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + 1 / self.rate

    def success(self) -> None:
        self._ok_streak += 1
        if self._ok_streak >= 200 and self.rate < self.base:
            self.rate = min(self.base, self.rate * 1.1)
            self._ok_streak = 0

    def flood(self) -> None:
        self._ok_streak = 0
        self.rate = max(0.3, self.rate * 0.7)


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
    requests: int = 0
    flood_waits: int = 0
    found: int = 0
    last_found_at: float | None = None
    last_error: str | None = None
    fatal: str | None = None


class Watcher:
    def __init__(self, client: TelegramClient, db: Database, on_upgrade: OnUpgrade, on_alert: OnAlert | None = None):
        self.client = client
        self.db = db
        self.on_upgrade = on_upgrade
        self.on_alert = on_alert
        self.limiter = RateLimiter(config.requests_per_second)
        self.metrics = Metrics()
        self.states: dict[int, CollectionState] = {}
        self._collections_updated = float("-inf")

    # ---------- запросы ----------

    async def _call(self, request):
        while True:
            await self.limiter.wait()
            self.metrics.requests += 1
            try:
                result = await self.client(request)
                self.limiter.success()
                return result
            except errors.FloodWaitError as e:
                self.metrics.flood_waits += 1
                self.limiter.flood()
                log.warning("FloodWait %s c, скорость снижена до %.2f запр/с", e.seconds, self.limiter.rate)
                await asyncio.sleep(e.seconds + 1)

    async def fetch(self, slug: str) -> types.payments.UniqueStarGift | None:
        try:
            resp = await self._call(functions.payments.GetUniqueStarGiftRequest(slug=slug))
        except FATAL_ERRORS:
            raise
        except errors.RPCError as e:
            if "SLUG_INVALID" not in str(e) and "NOT_FOUND" not in str(e):
                log.warning("Ошибка при запросе %s: %s", slug, e)
            return None
        return resp if isinstance(resp.gift, types.StarGiftUnique) else None

    # ---------- коллекции ----------

    async def load_states(self) -> None:
        for row in await self.db.collections():
            self.states.setdefault(
                row["gift_id"],
                CollectionState(row["gift_id"], row["title"], row["slug"], row["last_num"]),
            )

    async def refresh_collections(self) -> None:
        resp = await self._call(functions.payments.GetStarGiftsRequest(hash=0))
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
            resp = await self._call(
                functions.payments.GetResaleStarGiftsRequest(gift_id=state.gift_id, offset="", limit=1)
            )
        except FATAL_ERRORS:
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
                        resp = await self._call(request)
                    except FATAL_ERRORS:
                        raise
                    except errors.RPCError as e:
                        # Одна проблемная коллекция не должна мешать остальным.
                        log.debug("Атрибуты %s недоступны: %s", gift_id, e)
                        continue
                    await self.db.save_attributes(gift_id, parse_attributes(resp.attributes))
                    await asyncio.sleep(2)  # не мешаем основному опросу
            except asyncio.CancelledError:
                raise
            except FATAL_ERRORS:
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
                except FATAL_ERRORS as e:
                    self.metrics.fatal = type(e).__name__
                    log.error("Сессия наблюдателя больше не работает: %s", e)
                    if self.on_alert:
                        await self.on_alert(
                            f"⛔️ Аккаунт-наблюдатель отключён: {type(e).__name__}.\n"
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
