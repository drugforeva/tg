"""Каналы: подписку можно присылать в свой канал (бот — админ канала)."""

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Database
from ..render import esc
from .common import PREMIUM_HINT, is_premium, show

log = logging.getLogger(__name__)
router = Router(name="channels")

HOW_TO = (
    "Как подключить канал:\n"
    "1. Добавьте бота в администраторы канала с правом публиковать сообщения.\n"
    "2. Пришлите сюда <code>/connect @имя_канала</code> (или перешлите любой пост из канала).\n"
    "3. В редакторе подписки нажмите «📢 В канал» и выберите его."
)


async def connect_channel(message: Message, bot: Bot, db: Database, ref: str | int) -> None:
    if not await is_premium(db, message.chat.id):
        await message.answer(PREMIUM_HINT)
        return
    try:
        chat = await bot.get_chat(ref)
    except TelegramAPIError:
        await message.answer("Не нашёл такой канал. Проверьте имя и что бот добавлен в канал.\n\n" + HOW_TO)
        return
    if chat.type != ChatType.CHANNEL:
        await message.answer("Это не канал. Для групп просто добавьте бота в группу и настройте там /menu.")
        return
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat.id, me.id)
        user_member = await bot.get_chat_member(chat.id, message.from_user.id)
    except TelegramAPIError:
        await message.answer("Бот не видит участников канала — сделайте его администратором.\n\n" + HOW_TO)
        return
    if bot_member.status != "administrator" or not getattr(bot_member, "can_post_messages", False):
        await message.answer("Бот должен быть администратором канала с правом публиковать.\n\n" + HOW_TO)
        return
    if user_member.status not in ("creator", "administrator"):
        await message.answer("Подключать канал может только его администратор.")
        return
    await db.ensure_user(message.chat.id)
    await db.link_channel(chat.id, message.chat.id, chat.title or str(chat.id))
    await message.answer(
        f"📢 Канал «{esc(chat.title)}» подключён. Теперь в редакторе подписки нажмите «📢 В канал».",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⚙️ Меню", callback_data="menu")]]
        ),
    )


@router.message(Command("connect"), F.chat.type == ChatType.PRIVATE)
async def cmd_connect(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(HOW_TO)
        return
    ref = int(arg) if arg.lstrip("-").isdigit() else ("@" + arg.lstrip("@").split("/")[-1])
    await connect_channel(message, bot, db, ref)


@router.message(F.chat.type == ChatType.PRIVATE, F.forward_origin.type == "channel")
async def on_forward_from_channel(message: Message, bot: Bot, db: Database) -> None:
    await connect_channel(message, bot, db, message.forward_origin.chat.id)


async def channels_view(db: Database, owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    channels = await db.channels_of(owner_id)
    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.row(
            InlineKeyboardButton(text=f"📢 {ch['title']}"[:60], callback_data="noop"),
            InlineKeyboardButton(text="✖️ Отключить", callback_data=f"ch:del:{ch['channel_id']}"),
        )
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="menu"))
    text = "📢 <b>Каналы</b>\n\n"
    text += "Подключённых каналов нет.\n\n" + HOW_TO if not channels else "Подписки можно присылать в эти каналы."
    return text, kb.as_markup()


@router.message(Command("channels"))
async def cmd_channels(message: Message, db: Database) -> None:
    text, kb = await channels_view(db, message.chat.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("ch:"))
async def cb_channels(call: CallbackQuery, db: Database) -> None:
    parts = call.data.split(":")
    if parts[1] == "del":
        channel_id = int(parts[2])
        if channel_id in {r["channel_id"] for r in await db.channels_of(call.message.chat.id)}:
            await db.unlink_channel(channel_id)
    await show(call, *await channels_view(db, call.message.chat.id))


@router.my_chat_member()
async def on_bot_membership(update: ChatMemberUpdated, db: Database, bot: Bot) -> None:
    """Бота убрали из канала — отвязываем канал и предупреждаем владельца."""
    if update.chat.type != ChatType.CHANNEL:
        return
    if update.new_chat_member.status in ("left", "kicked"):
        for owner in await db.unlink_channel(update.chat.id):
            try:
                await bot.send_message(owner, f"⚠️ Бота убрали из канала «{esc(update.chat.title)}», он отключён.")
            except TelegramAPIError:
                log.info("Не удалось предупредить владельца канала")
