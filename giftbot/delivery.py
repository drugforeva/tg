"""Доставка уведомлений с учётом лимитов Bot API.

Подходящие подарки складываются в очередь чата. Если за время между отправками
набралось несколько — они уходят одним сообщением-сводкой, а не потоком карточек.
Интервал между сообщениями чат выбирает сам («мгновенно», «раз в 5 минут» и т.д.).
"""

import asyncio
import logging
import time
from collections import defaultdict

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from .config import config
from .db import Database
from .filters import matches
from .models import Upgrade
from .render import upgrade_card, upgrade_list
from .watcher import RateLimiter

log = logging.getLogger(__name__)

# Минимальная пауза между сообщениями в один чат (Telegram: ~1 сообщение/с на чат).
MIN_GAP = 2.0
# Больше этого в очереди одного чата не держим — старые выбрасываем.
MAX_PENDING = 300


def card_kb(u: Upgrade) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="🎁 Открыть", url=u.link)]
    if u.owner_username:
        row.append(InlineKeyboardButton(text="👤 Владелец", url=f"https://t.me/{u.owner_username}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row,
            [InlineKeyboardButton(text=f"🔕 Не присылать {u.collection}"[:60], callback_data=f"mute:{u.gift_id}")],
        ]
    )


class Delivery:
    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self.limiter = RateLimiter(25)  # общий лимит Bot API ~30 сообщений/с
        self.pending: dict[int, list[tuple[Upgrade, list[str]]]] = defaultdict(list)
        self.next_at: dict[int, float] = {}
        self.sent = 0

    async def on_upgrade(self, u: Upgrade) -> None:
        subs = await self.db.live_subscriptions()
        per_chat: dict[int, list[str]] = defaultdict(list)
        counts: dict[int, int] = defaultdict(int)
        for sub in subs:
            counts[sub.chat_id] += 1
            if matches(sub.filters, u):
                per_chat[sub.chat_id].append(sub.name)
        for chat_id, names in per_chat.items():
            queue = self.pending[chat_id]
            queue.append((u, names if counts[chat_id] > 1 else []))
            del queue[:-MAX_PENDING]

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
        if len(items) == 1:
            u, names = items[0]
            await self.send(
                chat_id,
                upgrade_card(u, names),
                link_preview_options=LinkPreviewOptions(url=u.link, prefer_large_media=True),
                reply_markup=card_kb(u),
            )
        else:
            upgrades = sorted((u for u, _ in items), key=lambda u: u.one_in or 0, reverse=True)
            await self.send(
                chat_id,
                upgrade_list(
                    f"🆕 <b>Новые улучшения: {len(items)}</b> (самые редкие сверху)",
                    upgrades,
                    footer="Слишком много? Сузьте фильтры или выберите частоту в /menu",
                ),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    async def flush_all(self) -> None:
        for chat_id in list(self.pending):
            await self.flush(chat_id)

    async def send(self, chat_id: int, text: str, **kwargs) -> bool:
        for _ in range(3):
            await self.limiter.wait()
            try:
                await self.bot.send_message(chat_id, text, **kwargs)
                self.sent += 1
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                await self.db.set_active(chat_id, False)  # бота заблокировали или удалили из чата
                return False
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    await self.db.set_active(chat_id, False)
                else:
                    log.warning("Не удалось отправить сообщение: %s", e)
                return False
            except Exception:
                log.exception("Не удалось отправить сообщение")
                return False
        return False

    async def alert_admins(self, text: str) -> None:
        for admin in config.admins:
            await self.send(admin, text)
