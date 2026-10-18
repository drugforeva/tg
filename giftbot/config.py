import os
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


@dataclass(frozen=True)
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    api_id: int = int(os.getenv("API_ID", "0") or 0)
    api_hash: str = os.getenv("API_HASH", "")
    session: str = os.getenv("SESSION", "watcher")
    # Сессия строкой (для GitHub Actions); если задана, файл сессии не нужен
    session_string: str = os.getenv("SESSION_STRING", "")
    # Через сколько секунд бот сам завершится (0 = работать бесконечно)
    run_seconds: float = float(os.getenv("RUN_SECONDS", "0") or 0)
    db_path: str = os.getenv("DB_PATH", "giftbot.db")
    poll_interval: float = float(os.getenv("POLL_INTERVAL", "15"))
    request_delay: float = float(os.getenv("REQUEST_DELAY", "0.35"))
    collections_refresh: float = float(os.getenv("COLLECTIONS_REFRESH", "1800"))
    slug_overrides: dict[str, str] = field(
        default_factory=lambda: _overrides(os.getenv("SLUG_OVERRIDES", ""))
    )
    admins: frozenset[int] = field(
        default_factory=lambda: frozenset(
            int(x) for x in os.getenv("ADMINS", "").replace(" ", "").split(",") if x
        )
    )


config = Config()
