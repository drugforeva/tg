import asyncio
import contextlib
import logging
import os
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


async def connect_clients(db: Database) -> list[tuple[object, str, str]]:
    """Аккаунты-наблюдатели: подключённые в боте (/accounts) и из настроек (SESSION_STRING / SESSION)."""
    sources: list[tuple[object, str, str]] = []  # (сессия, имя, ключ)
    for row in await db.accounts():
        sources.append((StringSession(row["session"]), f"#{row['id']} {row['name'] or ''}".strip(), f"db:{row['id']}"))
    for i, s in enumerate(config.session_strings, 1):
        sources.append((StringSession(s), f"env#{i}", f"env:{i}"))
    if not config.session_strings:
        for name in (n.strip() for n in config.session.split(",")):
            if name and os.path.exists(f"{name}.session"):
                sources.append((name, name, f"file:{name}"))
    clients = []
    for session, name, key in sources:
        # flood_sleep_threshold=0: все FloodWait обрабатывает пул аккаунтов.
        client = TelegramClient(session, config.api_id, config.api_hash, flood_sleep_threshold=0)
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
        except Exception as e:
            log.error("Аккаунт %s: не удалось подключиться: %s", name, e)
            continue
        if authorized:
            clients.append((client, name, key))
        else:
            log.error("Аккаунт %s не авторизован, пропускаю", name)
            await client.disconnect()
    return clients


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    if not config.bot_token:
        sys.exit("Заполните BOT_TOKEN в .env (см. .env.example)")

    db = Database(config.db_path)
    await db.connect()

    clients = await connect_clients(db)
    log.info("Аккаунтов-наблюдателей: %d", len(clients))

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    commands = COMMANDS + ([BotCommand(command="premium", description="Премиум")] if premium_enabled() else [])
    await bot.set_my_commands(commands)
    with contextlib.suppress(Exception):
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(DESCRIPTION[:120])

    delivery = Delivery(bot, db)

    async def on_dead(member) -> None:
        if member.key.startswith("db:"):
            await db.delete_account(int(member.key[3:]))

    # Без аккаунтов бот всё равно запускается: запросы ждут, пока админ подключит аккаунт в /accounts.
    pool = ClientPool([], config.requests_per_second, delivery.alert_admins, on_dead, wait_for_accounts=True)
    for client, name, key in clients:
        pool.add(client, name, key)
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
    if not clients:
        log.warning("Нет аккаунтов-наблюдателей: подключите их в боте командой /accounts")
        with contextlib.suppress(Exception):
            await delivery.alert_admins(
                "👋 Бот запущен, но аккаунтов-наблюдателей пока нет — без них он не видит улучшений и маркета.\n"
                "Подключите аккаунт по номеру телефона: /accounts"
            )
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
        for member in list(pool.members):
            with contextlib.suppress(Exception):
                await member.client.disconnect()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
