"""Доставка уведомлений с учётом лимитов Bot API.

Подходящие события складываются в очередь получателя (чат или канал). Если за
время между отправками набралось несколько — они уходят одним сообщением-сводкой,
а не потоком карточек. Интервал между сообщениями чат выбирает сам.
"""

import asyncio
import logging
import time
from collections import defaultdict

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from .config import config
from .db import Database
from .filters import matches
from .models import Upgrade
from .pool import RateLimiter
from .render import upgrade_card, upgrade_list

log = logging.getLogger(__name__)

# Минимальная пауза между сообщениями в один чат (Telegram: ~1 сообщение/с на чат).
MIN_GAP = 2.0
# Больше этого в очереди одного чата не держим — старые выбрасываем.
MAX_PENDING = 300


def card_kb(u: Upgrade, channel: bool = False) -> InlineKeyboardMarkup:
    open_text = "🛒 Купить" if u.event == "listing" else "🎁 Открыть"
    row = [InlineKeyboardButton(text=open_text, url=u.link)]
    if u.owner_username:
        row.append(InlineKeyboardButton(text="👤 Владелец", url=f"https://t.me/{u.owner_username}"))
    if channel:
        return InlineKeyboardMarkup(inline_keyboard=[row])
    follow = [InlineKeyboardButton(text="👁 Следить за подарком", callback_data=f"tw:add:{u.slug}"[:64])]
    if u.owner_username:
        follow.append(InlineKeyboardButton(text="👤 Следить за ним", callback_data=f"wo:{u.owner_username}"[:64]))
    mute = [InlineKeyboardButton(text=f"🔕 Не присылать {u.collection}"[:60], callback_data=f"mute:{u.gift_id}")]
    return InlineKeyboardMarkup(inline_keyboard=[row, follow, mute])


class Delivery:
    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self.limiter = RateLimiter(25)  # общий лимит Bot API ~30 сообщений/с
        self.pending: dict[int, list[tuple[Upgrade, list[str]]]] = defaultdict(list)
        self.next_at: dict[int, float] = {}
        self.channels: set[int] = set()
        self.sent = 0

    async def on_event(self, u: Upgrade) -> None:
        subs = await self.db.live_subscriptions()
        self.channels = await self.db.channel_ids()
        per_target: dict[int, list[str]] = defaultdict(list)
        counts: dict[int, int] = defaultdict(int)
        for sub in subs:
            counts[sub.target] += 1
            if matches(sub.filters, u):
                per_target[sub.target].append(sub.name)
        for target, names in per_target.items():
            queue = self.pending[target]
            if any(item.slug == u.slug and item.event == u.event for item, _ in queue):
                continue
            queue.append((u, names if counts[target] > 1 and target not in self.channels else []))
            del queue[:-MAX_PENDING]

    # Совместимость: наблюдатель вызывает on_upgrade.
    on_upgrade = on_event

    async def run(self) -> None:
        while True:
            now = time.monotonic()
            for chat_id in [c for c in self.pending if self.next_at.get(c, 0) <= now]:
                try:
                    await self.flush(chat_id)
                except Exception:
                    log.exception("Ошибка доставки")
            await asyncio.sleep(0.5)

    async def flush(self, chat_id: int) -> None:
        items = self.pending.pop(chat_id, [])
        if not items:
            return
        window = await self.db.get_window(chat_id)
        self.next_at[chat_id] = time.monotonic() + max(window, MIN_GAP)
        channel = chat_id in self.channels
        if len(items) == 1:
            u, names = items[0]
            await self.send(
                chat_id,
                upgrade_card(u, names),
                link_preview_options=LinkPreviewOptions(url=u.link, prefer_large_media=True),
                reply_markup=card_kb(u, channel),
            )
            return
        events = [u for u, _ in items]
        listings = [u for u in events if u.event == "listing"]
        upgrades = [u for u in events if u.event != "listing"]
        parts = []
        if upgrades:
            upgrades.sort(key=lambda u: u.one_in or 0, reverse=True)
            parts.append((f"🆕 <b>Новые улучшения: {len(upgrades)}</b> (самые редкие сверху)", upgrades))
        if listings:
            listings.sort(key=lambda u: u.discount if u.discount is not None else -999, reverse=True)
            parts.append((f"🏷 <b>Новые лоты на продаже: {len(listings)}</b> (самые выгодные сверху)", listings))
        footer = "" if channel else "Слишком много? Сузьте фильтры или выберите частоту в /menu"
        for title, group in parts:
            await self.send(
                chat_id,
                upgrade_list(title, group, footer=footer),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    async def flush_all(self) -> None:
        for chat_id in list(self.pending):
            await self.flush(chat_id)

    async def notify(self, chat_id: int, text: str, slug: str | None = None) -> bool:
        """Отдельное сообщение мимо очереди (слежка за подарком, служебное)."""
        kwargs = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        if slug:
            kwargs["reply_markup"] = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🎁 Открыть", url=f"https://t.me/nft/{slug}"),
                        InlineKeyboardButton(text="✖️ Не следить", callback_data=f"tw:del:{slug}"[:64]),
                    ]
                ]
            )
        return await self.send(chat_id, text, **kwargs)

    async def send(self, chat_id: int, text: str, document: str | None = None, **kwargs) -> bool:
        for _ in range(3):
            await self.limiter.wait()
            try:
                if document:
                    await self.bot.send_document(chat_id, FSInputFile(document), caption=text, **kwargs)
                else:
                    await self.bot.send_message(chat_id, text, **kwargs)
                self.sent += 1
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                await self._unreachable(chat_id)  # бота заблокировали или удалили из чата/канала
                return False
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    await self._unreachable(chat_id)
                else:
                    log.warning("Не удалось отправить сообщение: %s", e)
                return False
            except Exception:
                log.exception("Не удалось отправить сообщение")
                return False
        return False

    async def _unreachable(self, chat_id: int) -> None:
        if chat_id in self.channels or chat_id in await self.db.channel_ids():
            self.channels.discard(chat_id)
            for owner in await self.db.unlink_channel(chat_id):
                await self.send(owner, "⚠️ Бот потерял доступ к каналу, подписки снова присылают сюда. /channels")
        else:
            await self.db.set_active(chat_id, False)

    async def alert_admins(self, text: str) -> None:
        for admin in config.admins:
            await self.send(admin, text)
