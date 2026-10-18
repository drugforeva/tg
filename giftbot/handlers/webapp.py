"""Приём данных из Mini App (редактор фильтров, docs/index.html)."""

import json
import logging

from aiogram import F, Router
from aiogram.types import Message, ReplyKeyboardRemove

from ..db import Database
from ..filters import parse_numbers
from ..models import Filters
from .common import PREMIUM_HINT, is_premium
from .subscription import show_subscription

log = logging.getLogger(__name__)
router = Router(name="webapp")


@router.message(F.web_app_data)
async def on_webapp_data(message: Message, db: Database) -> None:
    try:
        data = json.loads(message.web_app_data.data)
        sub = await db.get_subscription(int(data["id"]))
    except (ValueError, KeyError, TypeError):
        await message.answer("Не удалось прочитать данные редактора.", reply_markup=ReplyKeyboardRemove())
        return
    if sub is None or sub.chat_id != message.chat.id:
        await message.answer("Подписка не найдена.", reply_markup=ReplyKeyboardRemove())
        return
    filters = Filters.sanitize(data.get("filters"))
    filters.numbers = parse_numbers(",".join(filters.numbers))
    note = ""
    if "listing" in filters.events and not await is_premium(db, sub.chat_id):
        filters.events = [e for e in filters.events if e != "listing"] or ["upgrade"]
        note = f"\n\nЛента продаж не включена. {PREMIUM_HINT}"
    sub.filters = filters
    if isinstance(data.get("name"), str) and data["name"].strip():
        sub.name = data["name"].strip()[:40]
    await db.save_subscription(sub)
    await message.answer("✅ Сохранено из редактора." + note, reply_markup=ReplyKeyboardRemove())
    await show_subscription(message, sub, db)
