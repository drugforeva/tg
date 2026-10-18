"""Общие помощники интерфейса: контекст приложения, лимиты премиума, вывод меню."""

import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from ..config import config
from ..db import Database

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
PAGE_SIZE = 20
PREMIUM_HINT = "⭐ Доступно в премиуме: /premium"


@dataclass
class App:
    """Всё, что нужно хендлерам помимо базы: наблюдатель, маркет, слежка, доставка, пул аккаунтов."""

    db: Database
    watcher: object
    delivery: object
    market: object = None
    tracker: object = None
    pool: object = None


class Edit(StatesGroup):
    waiting = State()


def flag(on: bool) -> str:
    return "✅" if on else "▫️"


async def can_manage(bot: Bot, chat, user_id: int) -> bool:
    """В группах настройки меняют только админы."""
    if chat.type == ChatType.PRIVATE:
        return True
    member = await bot.get_chat_member(chat.id, user_id)
    return member.status in ("creator", "administrator")


async def show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение с меню (для кнопок) или отправляет новое (для команд)."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                # Сообщение нельзя отредактировать (например, это фото) — шлём новое.
                await target.message.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


def chat_of(target: Message | CallbackQuery):
    return target.message.chat if isinstance(target, CallbackQuery) else target.chat


def nav(prefix: str, page: int, pages: int, suffix: str = "") -> list[InlineKeyboardButton]:
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{page - 1}{suffix}"))
    buttons.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{page + 1}{suffix}"))
    return buttons


def paginate(items: list, page: int, size: int = PAGE_SIZE) -> tuple[list, int, int]:
    pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    return items[page * size : (page + 1) * size], page, pages


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]])


# ---------- премиум ----------


def premium_enabled() -> bool:
    return config.premium_price > 0


async def is_premium(db: Database, chat_id: int) -> bool:
    if not premium_enabled() or chat_id in config.admins:
        return True
    return await db.premium_until(chat_id) > time.time()


async def subscription_limit(db: Database, chat_id: int) -> int:
    return config.max_subscriptions if await is_premium(db, chat_id) else config.free_subscriptions


async def tracked_limit(db: Database, chat_id: int) -> int:
    return config.max_tracked if await is_premium(db, chat_id) else config.free_tracked
