"""Задачи по расписанию: ежедневные сводки, бэкап базы админам, очистка истории."""

import asyncio
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import LinkPreviewOptions

from .config import config
from .db import Database
from .reports import daily_text

log = logging.getLogger(__name__)

PRUNE_EVERY = 6 * 3600


class Scheduler:
    def __init__(self, db: Database, delivery):
        self.db = db
        self.delivery = delivery
        self._pruned_at = float("-inf")
        self._backup_day: str | None = None

    async def tick(self, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        chats = await self.db.daily_due(now.hour, today)
        if chats:
            text = await daily_text(self.db)
            for chat_id in chats:
                await self.delivery.send(chat_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True))
                await self.db.mark_daily_sent(chat_id, today)
        if config.admins and config.backup_hour == now.hour and self._backup_day != today:
            self._backup_day = today
            await self.send_backup(f"💾 Ежедневный бэкап базы, {today}")
        if time.monotonic() - self._pruned_at > PRUNE_EVERY:
            self._pruned_at = time.monotonic()
            removed = await self.db.prune(config.keep_days * 86400)
            if removed:
                log.info("Удалено старых записей: %d", removed)

    async def send_backup(self, caption: str) -> None:
        from .handlers.admin import make_backup

        path = await make_backup(self.db)
        try:
            for admin in config.admins:
                await self.delivery.send(admin, caption, document=path)
        finally:
            os.remove(path)

    async def run(self) -> None:
        zone = ZoneInfo(config.timezone)
        while True:
            try:
                await self.tick(datetime.now(zone))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка задач по расписанию")
            await asyncio.sleep(30)
