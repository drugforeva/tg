import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from telethon import TelegramClient
from telethon.sessions import StringSession

from . import bot as bot_module
from .config import config
from .db import Database
from .watcher import Watcher


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not (config.bot_token and config.api_id and config.api_hash):
        sys.exit("Заполните BOT_TOKEN, API_ID и API_HASH в .env (см. .env.example)")

    db = Database(config.db_path)
    await db.connect()

    session = StringSession(config.session_string) if config.session_string else config.session
    client = TelegramClient(session, config.api_id, config.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Аккаунт-наблюдатель не авторизован. Выполните: python -m giftbot.login")

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="filters", description="Фильтры"),
        BotCommand(command="recent", description="Последние улучшения"),
        BotCommand(command="pause", description="Пауза уведомлений"),
        BotCommand(command="resume", description="Включить уведомления"),
    ])
    dp = Dispatcher()
    dp.include_router(bot_module.setup(db))

    watcher = Watcher(client, db, bot_module.Notifier(bot, db))
    watcher_task = asyncio.create_task(watcher.run())
    if config.run_seconds:
        async def stop_later() -> None:
            await asyncio.sleep(config.run_seconds)
            logging.info("Истекло RUN_SECONDS, завершаемся")
            await dp.stop_polling()

        asyncio.create_task(stop_later())
    try:
        await dp.start_polling(bot)
    finally:
        watcher_task.cancel()
        await client.disconnect()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
