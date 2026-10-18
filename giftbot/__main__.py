import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from telethon import TelegramClient
from telethon.sessions import StringSession

from .bot import router
from .config import config
from .db import Database
from .delivery import Delivery
from .watcher import Watcher

log = logging.getLogger("giftbot")

COMMANDS = [
    BotCommand(command="menu", description="Подписки и настройки"),
    BotCommand(command="recent", description="Последние улучшения"),
    BotCommand(command="top", description="Самые редкие за сутки"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="pause", description="Пауза уведомлений"),
    BotCommand(command="resume", description="Включить уведомления"),
    BotCommand(command="help", description="Помощь"),
]


async def maintenance(db: Database) -> None:
    while True:
        removed = await db.prune(config.keep_days * 86400)
        if removed:
            log.info("Удалено старых записей: %d", removed)
        await asyncio.sleep(6 * 3600)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    if not (config.bot_token and config.api_id and config.api_hash):
        sys.exit("Заполните BOT_TOKEN, API_ID и API_HASH в .env (см. .env.example)")

    db = Database(config.db_path)
    await db.connect()

    session = StringSession(config.session_string) if config.session_string else config.session
    # flood_sleep_threshold=0: все FloodWait обрабатывает наш ограничитель скорости.
    client = TelegramClient(session, config.api_id, config.api_hash, flood_sleep_threshold=0)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Аккаунт-наблюдатель не авторизован. Выполните: python -m giftbot.login")

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands(COMMANDS)

    delivery = Delivery(bot, db)
    watcher = Watcher(client, db, delivery.on_upgrade, delivery.alert_admins)
    dp = Dispatcher(db=db, watcher=watcher, delivery=delivery)
    dp.include_router(router)

    tasks = [
        asyncio.create_task(watcher.run()),
        asyncio.create_task(delivery.run()),
        asyncio.create_task(maintenance(db)),
    ]
    if config.run_seconds:

        async def stop_later() -> None:
            await asyncio.sleep(config.run_seconds)
            log.info("Истекло RUN_SECONDS, завершаемся")
            await dp.stop_polling()

        tasks.append(asyncio.create_task(stop_later()))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await delivery.flush_all()  # не теряем то, что успели найти
        await client.disconnect()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
