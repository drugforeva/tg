import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _overrides(raw: str) -> dict[str, str]:
    result = {}
    for pair in raw.split(";"):
        if "=" in pair:
            title, slug = pair.split("=", 1)
            result[title.strip()] = slug.strip()
    return result


def _str(name: str, default: str = "") -> str:
    # Пустая переменная (например, незаданная в GitHub Actions) — значит «по умолчанию».
    return os.getenv(name) or default


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, "") or default)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, "") or default)


def _sessions(raw: str) -> tuple[str, ...]:
    # Несколько аккаунтов-наблюдателей: строки сессий через пробел, запятую или перевод строки.
    return tuple(s for s in re.split(r"[\s,]+", raw) if s)


@dataclass(frozen=True)
class Config:
    bot_token: str = _str("BOT_TOKEN")
    api_id: int = _int("API_ID", 0)
    api_hash: str = _str("API_HASH")
    session: str = _str("SESSION", "watcher")
    # Сессии строками (для GitHub Actions); если заданы, файл сессии не нужен
    session_strings: tuple[str, ...] = field(default_factory=lambda: _sessions(_str("SESSION_STRING", "")))
    # Через сколько секунд бот сам завершится (0 = работать бесконечно)
    run_seconds: float = _float("RUN_SECONDS", 0)
    db_path: str = _str("DB_PATH", "giftbot.db")
    # Сколько запросов в секунду делает каждый аккаунт-наблюдатель (при FloodWait снижается сам)
    requests_per_second: float = _float("REQUESTS_PER_SECOND", 3)
    # Интервал опроса одной коллекции: активные — часто, тихие — всё реже, до POLL_MAX
    poll_min: float = _float("POLL_MIN", 4)
    poll_max: float = _float("POLL_MAX", 90)
    collections_refresh: float = _float("COLLECTIONS_REFRESH", 1800)
    # Как часто обновлять флоры и число лотов на маркете (один запрос на все коллекции)
    market_refresh: float = _float("MARKET_REFRESH", 30)
    # Даже без изменений перепроверять ленту продаж коллекции не реже, чем раз в столько секунд
    market_poll_max: float = _float("MARKET_POLL_MAX", 300)
    # Как часто проверять подарки, за которыми следят пользователи
    track_interval: float = _float("TRACK_INTERVAL", 120)
    # Сколько дней хранить историю улучшений, продаж и флоров
    keep_days: float = _float("KEEP_DAYS", 7)
    # Часовой пояс для ежедневных сводок и бэкапов
    timezone: str = _str("TIMEZONE", "Europe/Moscow")
    # Во сколько присылать админам бэкап базы (-1 = не присылать)
    backup_hour: int = _int("BACKUP_HOUR", 4)
    # Адрес Mini App с редактором фильтров (например GitHub Pages); пусто — кнопки не будет
    webapp_url: str = _str("WEBAPP_URL", "")
    # Премиум за Telegram Stars: цена за период (0 = премиум выключен, всё бесплатно)
    premium_price: int = _int("PREMIUM_PRICE", 0)
    premium_days: int = _int("PREMIUM_DAYS", 30)
    free_subscriptions: int = _int("FREE_SUBSCRIPTIONS", 2)
    free_tracked: int = _int("FREE_TRACKED", 3)
    max_subscriptions: int = _int("MAX_SUBSCRIPTIONS", 10)
    max_tracked: int = _int("MAX_TRACKED", 50)
    slug_overrides: dict[str, str] = field(default_factory=lambda: _overrides(_str("SLUG_OVERRIDES", "")))
    admins: frozenset[int] = field(
        default_factory=lambda: frozenset(int(x) for x in _str("ADMINS", "").replace(" ", "").split(",") if x)
    )


config = Config()
