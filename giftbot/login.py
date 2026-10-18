"""Однократный вход в аккаунт-наблюдатель: python -m giftbot.login

Сохраняет файл сессии для локального запуска и печатает SESSION_STRING
для GitHub Actions. Чтобы добавить ещё один аккаунт, выполните
SESSION=watcher2 python -m giftbot.login и допишите новую строку
в SESSION_STRING через пробел.
"""

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from .config import config


async def main() -> None:
    session = config.session.split(",")[0].strip()
    client = TelegramClient(session, config.api_id, config.api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Готово, сессия '{session}.session' сохранена для {me.first_name} (id {me.id})")
    print("\nSESSION_STRING для GitHub Actions (никому не показывайте!).")
    print("Для нескольких аккаунтов перечислите их строки через пробел.\n")
    print(StringSession.save(client.session))
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
