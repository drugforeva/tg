"""Наблюдатель: через аккаунт Telegram (MTProto) находит только что улучшенные подарки.

Как это работает. Улучшенные подарки нумеруются по порядку внутри коллекции
(t.me/nft/PlushPepe-1, PlushPepe-2, ...), а каждый из них сообщает, сколько
экземпляров коллекции уже выпущено (availability_issued). Раз в проход мы
запрашиваем последний известный номер, узнаём текущее issued и докачиваем все
номера между ними — это и есть новые улучшения.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

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

# Сколько новых номеров одной коллекции докачивать за проход.
MAX_FETCH_PER_PASS = 40

OnUpgrade = Callable[[Upgrade], Awaitable[None]]


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
        detected_at=int(time.time()),
    )
    for attr in gift.attributes:
        if isinstance(attr, types.StarGiftAttributeModel):
            u.model, u.model_rarity = attr.name, _rarity(attr)
        elif isinstance(attr, types.StarGiftAttributeBackdrop):
            u.backdrop, u.backdrop_rarity = attr.name, _rarity(attr)
        elif isinstance(attr, types.StarGiftAttributePattern):
            u.symbol, u.symbol_rarity = attr.name, _rarity(attr)

    owner_id = _peer_id(gift.owner_id)
    if owner_id is not None:
        u.owner_id = owner_id
        entities = {e.id: e for e in [*resp.users, *resp.chats]}
        if owner := entities.get(owner_id):
            u.owner_username = _username(owner)
            u.owner_name = _display_name(owner) or u.owner_name

    for amount in gift.resell_amount or []:
        if isinstance(amount, types.StarsAmount):
            u.resale_stars = amount.amount
            break
    return u


class Watcher:
    def __init__(self, client: TelegramClient, db: Database, on_upgrade: OnUpgrade):
        self.client = client
        self.db = db
        self.on_upgrade = on_upgrade
        self._collections_updated = 0.0
        self._warned: set[str] = set()

    async def _call(self, request):
        await asyncio.sleep(config.request_delay)
        while True:
            try:
                return await self.client(request)
            except errors.FloodWaitError as e:
                log.warning("FloodWait %s c, ждём", e.seconds)
                await asyncio.sleep(e.seconds + 1)

    async def fetch(self, slug: str) -> types.payments.UniqueStarGift | None:
        try:
            return await self._call(functions.payments.GetUniqueStarGiftRequest(slug=slug))
        except errors.RPCError as e:
            if "SLUG_INVALID" in str(e) or "NOT_FOUND" in str(e):
                return None
            log.warning("Ошибка при запросе %s: %s", slug, e)
            return None

    async def refresh_collections(self) -> None:
        resp = await self._call(functions.payments.GetStarGiftsRequest(hash=0))
        count = 0
        for gift in resp.gifts:
            if not isinstance(gift, types.StarGift) or gift.upgrade_stars is None:
                continue
            title = gift.title
            if not title:
                continue
            slug = config.slug_overrides.get(title) or title_to_slug(title)
            await self.db.upsert_collection(gift.id, title, slug, gift.availability_total)
            count += 1
        self._collections_updated = time.monotonic()
        log.info("Коллекций с улучшением: %d", count)

    async def check_collection(self, row) -> int:
        gift_id, title, slug, last_num = row["gift_id"], row["title"], row["slug"], row["last_num"]

        anchor = await self.fetch(f"{slug}-{max(last_num or 1, 1)}")
        if anchor is None and last_num:
            anchor = await self.fetch(f"{slug}-1")
        if anchor is None:
            if slug not in self._warned:
                self._warned.add(slug)
                log.info("%s: улучшенных экземпляров пока нет или неверный слаг '%s'", title, slug)
            if last_num is None:
                await self.db.set_last_num(gift_id, 0)
            return 0

        issued = anchor.gift.availability_issued
        if last_num is None:
            # Первый запуск: историю не рассылаем, просто запоминаем позицию.
            await self.db.set_last_num(gift_id, issued)
            log.info("%s: стартуем с номера %d", title, issued)
            return 0
        if issued <= last_num:
            return 0

        # Большой отрыв догоняем порциями за несколько проходов, ничего не пропуская.
        end = min(issued, last_num + MAX_FETCH_PER_PASS)
        found = 0
        for num in range(last_num + 1, end + 1):
            resp = await self.fetch(f"{slug}-{num}")
            if resp is None or not isinstance(resp.gift, types.StarGiftUnique):
                # Номер ещё не доступен — попробуем в следующем проходе.
                break
            upgrade = parse_unique(resp, title)
            if await self.db.save_upgrade(upgrade):
                found += 1
                await self.on_upgrade(upgrade)
            await self.db.set_last_num(gift_id, num)
        return found

    async def run(self) -> None:
        while True:
            try:
                if time.monotonic() - self._collections_updated > config.collections_refresh:
                    await self.refresh_collections()
                for row in await self.db.collections():
                    await self.check_collection(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка в цикле наблюдателя")
            await asyncio.sleep(config.poll_interval)
