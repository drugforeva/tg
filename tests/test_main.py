import asyncio
import dataclasses

from aiogram import Bot
from conftest import FakeClient, FakeSession

import giftbot.__main__ as app


class FakeTelegramClient(FakeClient):
    def __init__(self, *args, **kwargs):
        super().__init__()

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        pass


async def test_main_runs_and_stops_by_itself(tmp_path, monkeypatch, fast_config):
    cfg = dataclasses.replace(
        fast_config, bot_token="42:TEST", api_id=1, api_hash="x", run_seconds=1.5, db_path=str(tmp_path / "bot.db")
    )
    monkeypatch.setattr(app, "config", cfg)
    monkeypatch.setattr(app, "TelegramClient", FakeTelegramClient)
    session = FakeSession()
    monkeypatch.setattr(app, "Bot", lambda token, **kw: Bot(token, session=session, **kw))

    await asyncio.wait_for(app.main(), timeout=20)

    names = {type(r).__name__ for r in session.requests}
    assert {"SetMyCommands", "GetMe", "GetUpdates"} <= names
    # За время работы наблюдатель загрузил коллекцию и запомнил позицию.
    from giftbot.db import Database

    db = Database(cfg.db_path)
    await db.connect()
    try:
        rows = await db.collections()
        assert rows[0]["title"] == "Plush Pepe" and rows[0]["last_num"] == 10
        assert await db.attribute_options("model", None) == [(100, "Gold", 8)]
    finally:
        await db.close()
