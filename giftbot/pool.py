"""Пул аккаунтов-наблюдателей с общим ограничением скорости.

Запросы распределяются между аккаунтами: каждый раз берётся тот, кто раньше
освободится. FloodWait на одном аккаунте временно выключает только его,
а умерший аккаунт (сессия отозвана и т.п.) убирается из пула с уведомлением.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from telethon import errors

log = logging.getLogger(__name__)

# Ошибки, после которых аккаунт работать не может.
FATAL_ERRORS = (
    errors.AuthKeyUnregisteredError,
    errors.AuthKeyDuplicatedError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
)


class PoolExhausted(Exception):
    """В пуле не осталось рабочих аккаунтов."""


class RateLimiter:
    """Равномерно распределяет запросы; после FloodWait снижает скорость и потом плавно возвращает."""

    def __init__(self, rate: float):
        self.base = self.rate = rate
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._ok_streak = 0

    @property
    def next_free(self) -> float:
        return self._next

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + 1 / self.rate

    def success(self) -> None:
        self._ok_streak += 1
        if self._ok_streak >= 200 and self.rate < self.base:
            self.rate = min(self.base, self.rate * 1.1)
            self._ok_streak = 0

    def flood(self, seconds: float = 0) -> None:
        self._ok_streak = 0
        self.rate = max(0.3, self.rate * 0.7)
        self._next = max(self._next, time.monotonic() + seconds)


@dataclass
class Member:
    client: object
    name: str
    limiter: RateLimiter
    key: str = ""  # "db:<id>" — аккаунт, подключённый через бота; иначе из настроек
    requests: int = 0
    flood_waits: int = 0


@dataclass
class PoolMetrics:
    requests: int = 0
    flood_waits: int = 0
    dead: list[str] = field(default_factory=list)


class ClientPool:
    """Пул аккаунтов. Если аккаунтов нет, запросы ждут, пока админ подключит аккаунт в боте."""

    def __init__(
        self,
        clients: list,
        rate: float,
        on_alert: Callable[[str], Awaitable[None]] | None = None,
        on_dead: Callable[[Member], Awaitable[None]] | None = None,
        wait_for_accounts: bool = False,
    ):
        self.rate_per_account = rate
        self.members: list[Member] = []
        self.on_alert = on_alert
        self.on_dead = on_dead
        self.wait_for_accounts = wait_for_accounts
        self.metrics = PoolMetrics()
        self._available = asyncio.Event()
        for i, c in enumerate(clients):
            self.add(c, f"#{i + 1}")

    @property
    def rate(self) -> float:
        return sum(m.limiter.rate for m in self.members)

    @property
    def ready(self) -> bool:
        return bool(self.members)

    def add(self, client, name: str, key: str = "") -> Member:
        member = Member(client, name, RateLimiter(self.rate_per_account), key)
        self.members.append(member)
        self._available.set()
        return member

    def remove(self, key: str) -> Member | None:
        member = next((m for m in self.members if m.key == key), None)
        if member:
            self.members.remove(member)
            if not self.members:
                self._available.clear()
        return member

    async def __call__(self, request):
        while True:
            if not self.members:
                if not self.wait_for_accounts:
                    raise PoolExhausted("нет рабочих аккаунтов-наблюдателей")
                await self._available.wait()
                continue
            member = min(self.members, key=lambda m: m.limiter.next_free)
            await member.limiter.wait()
            member.requests += 1
            self.metrics.requests += 1
            try:
                result = await member.client(request)
            except errors.FloodWaitError as e:
                member.flood_waits += 1
                self.metrics.flood_waits += 1
                member.limiter.flood(e.seconds + 1)
                log.warning("Аккаунт %s: FloodWait %s c, переключаемся", member.name, e.seconds)
                continue
            except FATAL_ERRORS as e:
                await self._remove(member, e)
                continue
            member.limiter.success()
            return result

    async def _remove(self, member: Member, error: Exception) -> None:
        if member not in self.members:
            return
        self.members.remove(member)
        if not self.members:
            self._available.clear()
        reason = type(error).__name__
        self.metrics.dead.append(f"{member.name}: {reason}")
        log.error("Аккаунт-наблюдатель %s отключён: %s", member.name, reason)
        if self.on_dead:
            await self.on_dead(member)
        if self.on_alert:
            left = len(self.members)
            await self.on_alert(
                f"⛔️ Аккаунт-наблюдатель {member.name} отключён: {reason}. Осталось рабочих: {left}.\n"
                "Подключите аккаунт заново: /accounts"
            )
