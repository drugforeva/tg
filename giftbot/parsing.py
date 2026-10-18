"""Разбор ответов MTProto о подарках."""

import re
import time

from telethon.tl import types

from .models import Upgrade

# Подарки после крафта получают уровень редкости без точного процента —
# переводим уровень в примерные промилле, чтобы работали фильтры по редкости.
TIER_PERMILLE = {
    types.StarGiftAttributeRarityLegendary: 5,
    types.StarGiftAttributeRarityEpic: 15,
    types.StarGiftAttributeRarityRare: 30,
    types.StarGiftAttributeRarityUncommon: 100,
}


def title_to_slug(title: str) -> str:
    """'Plush Pepe' -> 'PlushPepe', "Durov's Cap" -> 'DurovsCap', 'Jack-in-the-Box' -> 'JackInTheBox'."""
    cleaned = re.sub(r"['’`]", "", title)
    words = re.split(r"[^0-9A-Za-z]+", cleaned)
    return "".join(w[:1].upper() + w[1:] for w in words if w)


def parse_slug(text: str) -> str | None:
    """Ссылка t.me/nft/PlushPepe-123 (в любом месте текста) или просто PlushPepe-123 -> 'PlushPepe-123'."""
    match = re.search(r"nft/([A-Za-z0-9]+-\d+)", text) or re.fullmatch(r"\s*([A-Za-z0-9]+-\d+)\s*", text)
    return match.group(1) if match else None


def rarity(attr) -> int | None:
    permille = getattr(attr, "rarity_permille", None)
    if permille is not None:
        return permille
    value = getattr(attr, "rarity", None)
    if isinstance(value, types.StarGiftAttributeRarity):
        return value.permille
    return TIER_PERMILLE.get(type(value))


def peer_id(peer) -> int | None:
    if isinstance(peer, types.PeerUser):
        return peer.user_id
    if isinstance(peer, types.PeerChannel):
        return peer.channel_id
    if isinstance(peer, types.PeerChat):
        return peer.chat_id
    return None


def username(entity) -> str | None:
    if getattr(entity, "username", None):
        return entity.username
    for u in getattr(entity, "usernames", None) or []:
        if u.active:
            return u.username
    return None


def display_name(entity) -> str | None:
    if isinstance(entity, types.User):
        return " ".join(x for x in (entity.first_name, entity.last_name) if x) or None
    return getattr(entity, "title", None)


def parse_attributes(attributes) -> list[tuple[str, int, str, int | None]]:
    """Атрибуты коллекции -> [(kind, attr_id, name, rarity)]."""
    items = []
    for attr in attributes:
        if isinstance(attr, types.StarGiftAttributeModel):
            items.append(("model", attr.document.id, attr.name, rarity(attr)))
        elif isinstance(attr, types.StarGiftAttributeBackdrop):
            items.append(("backdrop", attr.backdrop_id, attr.name, rarity(attr)))
        elif isinstance(attr, types.StarGiftAttributePattern):
            items.append(("symbol", attr.document.id, attr.name, rarity(attr)))
    return items


def parse_gift(gift: types.StarGiftUnique, entities: dict, collection: str, event: str = "upgrade") -> Upgrade:
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
        event=event,
    )
    sender_peer = None
    for attr in gift.attributes:
        if isinstance(attr, types.StarGiftAttributeModel):
            u.model, u.model_rarity = attr.name, rarity(attr)
        elif isinstance(attr, types.StarGiftAttributeBackdrop):
            u.backdrop, u.backdrop_rarity = attr.name, rarity(attr)
        elif isinstance(attr, types.StarGiftAttributePattern):
            u.symbol, u.symbol_rarity = attr.name, rarity(attr)
        elif isinstance(attr, types.StarGiftAttributeOriginalDetails):
            sender_peer = attr.sender_id

    if (owner_id := peer_id(gift.owner_id)) is not None:
        u.owner_id = owner_id
        if owner := entities.get(owner_id):
            u.owner_username = username(owner)
            u.owner_name = display_name(owner) or u.owner_name

    if (sender_id := peer_id(sender_peer)) is not None and sender_id != u.owner_id:
        u.sender_id = sender_id
        if sender := entities.get(sender_id):
            u.sender_username = username(sender)
            u.sender_name = display_name(sender)

    for amount in gift.resell_amount or []:
        if isinstance(amount, types.StarsTonAmount):
            u.resale_ton = amount.amount
        elif isinstance(amount, types.StarsAmount) and not gift.resale_ton_only:
            u.resale_stars = amount.amount
    return u


def entities_of(resp) -> dict:
    return {e.id: e for e in [*getattr(resp, "users", []), *getattr(resp, "chats", [])]}


def parse_unique(resp: types.payments.UniqueStarGift, collection: str) -> Upgrade:
    return parse_gift(resp.gift, entities_of(resp), collection)
