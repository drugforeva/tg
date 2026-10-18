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

from . import handlers
from .config import config
from .db import Database
from .delivery import Delivery
from .handlers.common import App, premium_enabled
from .market import Market
from .pool import ClientPool
from .scheduler import Scheduler
from .tracker import Tracker
from .watcher import Watcher

log = logging.getLogger("giftbot")

COMMANDS = [
    BotCommand(command="menu", description="Подписки и настройки"),
    BotCommand(command="recent", description="Последнее по подпискам"),
    BotCommand(command="top", description="Самые редкие за сутки"),
    BotCommand(command="deals", description="Выгодные лоты за сутки"),
    BotCommand(command="collection", description="Статистика коллекции"),
    BotCommand(command="watches", description="Слежка за подарками"),
    BotCommand(command="channels", description="Мои каналы"),
    BotCommand(command="daily", description="Ежедневная сводка"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="help", description="Помощь"),
]
DESCRIPTION = (
    "Слежу за подарками Telegram: присылаю только что улучшенные, новые лоты на маркете и те, "
    "что дешевле флора. Фильтры по коллекциям, моделям, фонам, узорам, редкости, номерам и цене."
)


async def connect_clients() -> list[TelegramClient]:
    """Аккаунты-наблюдатели: строки из SESSION_STRING или файлы из SESSION (через запятую)."""
    sessions = [StringSession(s) for s in config.session_strings] or [
        name.strip() for name in config.session.split(",") if name.strip()
    ]
    clients = []
    for i, session in enumerate(sessions, 1):
        # flood_sleep_threshold=0: все FloodWait обрабатывает пул аккаунтов.
        client = TelegramClient(session, config.api_id, config.api_hash, flood_sleep_threshold=0)
        await client.connect()
        if await client.is_user_authorized():
            clients.append(client)
        else:
            log.error("Аккаунт-наблюдатель #%d не авторизован, пропускаю", i)
            await client.disconnect()
    return clients


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    if not (config.bot_token and config.api_id and config.api_hash):
        sys.exit("Заполните BOT_TOKEN, API_ID и API_HASH в .env (см. .env.example)")

    db = Database(config.db_path)
    await db.connect()

    clients = await connect_clients()
    if not clients:
        sys.exit("Нет авторизованных аккаунтов-наблюдателей. Выполните: python -m giftbot.login")
    log.info("Аккаунтов-наблюдателей: %d", len(clients))

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    commands = COMMANDS + ([BotCommand(command="premium", description="Премиум")] if premium_enabled() else [])
    await bot.set_my_commands(commands)
    with contextlib.suppress(Exception):
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(DESCRIPTION[:120])

    delivery = Delivery(bot, db)
    pool = ClientPool(clients, config.requests_per_second, on_alert=delivery.alert_admins)
    market = Market(pool, db, delivery.on_event)
    watcher = Watcher(pool, db, delivery.on_event, delivery.alert_admins, floor_of=market.floor_of)
    tracker = Tracker(pool, db, delivery.notify)
    app = App(db=db, watcher=watcher, delivery=delivery, market=market, tracker=tracker, pool=pool)

    dp = Dispatcher(db=db, app=app)
    handlers.setup(dp)

    tasks = [
        asyncio.create_task(watcher.run()),
        asyncio.create_task(market.run()),
        asyncio.create_task(tracker.run()),
        asyncio.create_task(delivery.run()),
        asyncio.create_task(Scheduler(db, delivery).run()),
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
        for client in clients:
            await client.disconnect()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
