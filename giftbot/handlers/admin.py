"""Команды админа: бэкап базы и рассылка."""

import asyncio
import os
import tempfile

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ..config import config
from ..db import Database
from .common import App

router = Router(name="admin")


async def make_backup(db: Database) -> str:
    path = os.path.join(tempfile.mkdtemp(), "giftbot-backup.db")
    await db.backup(path)
    return path


@router.message(Command("backup"))
async def cmd_backup(message: Message, db: Database, app: App) -> None:
    if message.from_user.id not in config.admins:
        return
    path = await make_backup(db)
    try:
        await app.delivery.send(message.chat.id, "💾 Бэкап базы", document=path)
    finally:
        os.remove(path)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, db: Database, app: App) -> None:
    """/broadcast текст — разослать сообщение всем активным чатам."""
    if message.from_user.id not in config.admins:
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: /broadcast текст сообщения (HTML можно)")
        return
    chats = await db.active_chats()
    sent = 0
    for chat_id in chats:
        if await app.delivery.send(chat_id, text):
            sent += 1
        await asyncio.sleep(0.05)
    await message.answer(f"📣 Разослано: {sent} из {len(chats)}")
