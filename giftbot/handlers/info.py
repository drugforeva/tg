"""История, топы, статистика, карточки коллекций с графиком."""

import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..charts import collection_chart
from ..config import config
from ..db import Database
from ..reports import collection_text, deals_text, recent_text, stats_text, top_text
from .common import NO_PREVIEW, App, back_to_menu, nav, paginate, show

router = Router(name="info")
CHART_HOURS = 48


@router.message(Command("recent"))
async def cmd_recent(message: Message, command: CommandObject, db: Database) -> None:
    try:
        limit = max(1, min(int(command.args or 15), 30))
    except ValueError:
        limit = 15
    await message.answer(await recent_text(db, message.chat.id, limit), link_preview_options=NO_PREVIEW)


@router.message(Command("top"))
async def cmd_top(message: Message, db: Database) -> None:
    await message.answer(await top_text(db), link_preview_options=NO_PREVIEW)


@router.message(Command("deals"))
async def cmd_deals(message: Message, db: Database) -> None:
    await message.answer(await deals_text(db), link_preview_options=NO_PREVIEW)


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, app: App) -> None:
    await message.answer(await stats_text(db, app, message.from_user.id in config.admins))


@router.callback_query(F.data.in_({"recent", "top", "deals", "stats"}))
async def cb_info(call: CallbackQuery, db: Database, app: App) -> None:
    if call.data == "recent":
        text = await recent_text(db, call.message.chat.id, 15)
    elif call.data == "top":
        text = await top_text(db)
    elif call.data == "deals":
        text = await deals_text(db)
    else:
        text = await stats_text(db, app, call.from_user.id in config.admins)
    await show(call, text, back_to_menu())


async def send_collection(message: Message, db: Database, row) -> None:
    now = int(time.time())
    text = await collection_text(db, row)
    png = collection_chart(
        row["title"],
        await db.hourly_upgrades(row["gift_id"], now - CHART_HOURS * 3600),
        await db.floor_history(row["gift_id"], now - CHART_HOURS * 3600),
        config.timezone,
    )
    if png and len(text) <= 1024:
        await message.answer_photo(BufferedInputFile(png, "chart.png"), caption=text)
    else:
        await message.answer(text, link_preview_options=NO_PREVIEW)


async def collections_list(target: Message | CallbackQuery, db: Database, page: int) -> None:
    rows, page, pages = paginate(await db.collections(), page)
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=r["title"], callback_data=f"col:{r['gift_id']}")
    kb.adjust(2)
    kb.row(*nav("col:list", page, pages))
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="menu"))
    text = "📈 <b>Коллекции</b>\nВыберите коллекцию — покажу темп улучшений, флор и график."
    if not rows:
        text += "\n\nСписок ещё загружается."
    await show(target, text, kb.as_markup())


@router.message(Command("collection"))
async def cmd_collection(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args:
        await collections_list(message, db, 0)
        return
    rows = await db.find_collections(command.args)
    if not rows:
        await message.answer("Не нашёл такую коллекцию. /collection без названия — список.")
    elif len(rows) == 1:
        await send_collection(message, db, rows[0])
    else:
        kb = InlineKeyboardBuilder()
        for r in rows[:20]:
            kb.button(text=r["title"], callback_data=f"col:{r['gift_id']}")
        kb.adjust(2)
        await message.answer("Нашлось несколько, выберите:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("col:"))
async def cb_collection(call: CallbackQuery, db: Database) -> None:
    parts = call.data.split(":")
    if parts[1] == "list":
        await collections_list(call, db, int(parts[2]))
        return
    row = await db.collection(int(parts[1]))
    if not row:
        await call.answer("Коллекция не найдена")
        return
    await call.answer()
    await send_collection(call.message, db, row)
