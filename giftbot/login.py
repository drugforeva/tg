"""Однократный вход в аккаунт-наблюдатель: python -m giftbot.login

Сохраняет файл сессии для локального запуска и печатает SESSION_STRING
для GitHub Actions.
"""

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from .config import config


async def main() -> None:
    client = TelegramClient(config.session, config.api_id, config.api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Готово, сессия '{config.session}.session' сохранена для {me.first_name} (id {me.id})")
    print("\nSESSION_STRING для GitHub Actions (никому не показывайте!):\n")
    print(StringSession.save(client.session))
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
