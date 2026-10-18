from dataclasses import asdict, dataclass, field, fields

EVENTS = ("upgrade", "listing")


@dataclass
class Upgrade:
    """Событие с уникальным подарком: улучшение (event=upgrade) или выставление на продажу (listing)."""

    slug: str
    collection: str
    gift_id: int
    num: int
    issued: int
    total: int
    model: str | None = None
    model_rarity: int | None = None  # промилле (10 = 1%)
    backdrop: str | None = None
    backdrop_rarity: int | None = None
    symbol: str | None = None
    symbol_rarity: int | None = None
    owner_id: int | None = None
    owner_name: str | None = None
    owner_username: str | None = None
    owner_address: str | None = None
    sender_id: int | None = None
    sender_name: str | None = None
    sender_username: str | None = None
    resale_stars: int | None = None
    resale_ton: int | None = None  # нанотоны
    value_usd: int | None = None  # центы
    crafted: bool = False
    detected_at: int = 0
    event: str = "upgrade"
    floor_stars: int | None = None  # флор коллекции на момент события

    @property
    def link(self) -> str:
        return f"https://t.me/nft/{self.slug}"

    @property
    def one_in(self) -> int | None:
        """Насколько редкое сочетание модель + фон + узор: «1 из N»."""
        parts = (self.model_rarity, self.backdrop_rarity, self.symbol_rarity)
        if any(not p for p in parts):
            return None
        chance = parts[0] * parts[1] * parts[2] / 1e9
        return round(1 / chance)

    @property
    def discount(self) -> int | None:
        """На сколько процентов цена ниже флора коллекции (отрицательное — дороже)."""
        if not self.resale_stars or not self.floor_stars:
            return None
        return round((self.floor_stars - self.resale_stars) * 100 / self.floor_stars)

    def to_row(self) -> dict:
        return asdict(self)


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip()[:64] for x in value if str(x).strip()][:200]


def _opt_int(value, lo: int = 0, hi: int = 10**12) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if lo < number <= hi else None


@dataclass
class Filters:
    events: list[str] = field(default_factory=lambda: ["upgrade"])
    collections: list[str] = field(default_factory=list)  # пусто = все
    exclude_collections: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)  # названия, можно с *, пусто = любые
    backdrops: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    exclude_models: list[str] = field(default_factory=list)
    exclude_backdrops: list[str] = field(default_factory=list)
    exclude_symbols: list[str] = field(default_factory=list)
    max_model_rarity: int | None = None  # промилле
    max_backdrop_rarity: int | None = None
    max_symbol_rarity: int | None = None
    min_one_in: int | None = None  # редкость сочетания не хуже «1 из N»
    max_num: int | None = None
    pretty_num: bool = False
    numbers: list[str] = field(default_factory=list)  # «777», «1-100»
    owners: list[str] = field(default_factory=list)  # @username или id владельца/отправителя
    with_username: bool = False
    on_sale: bool = False
    max_price: int | None = None  # звёзды
    min_discount: int | None = None  # % ниже флора

    @classmethod
    def from_dict(cls, data: dict) -> "Filters":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @classmethod
    def sanitize(cls, data) -> "Filters":
        """Собирает фильтры из непроверенных данных (Mini App), приводя типы и отбрасывая мусор."""
        if not isinstance(data, dict):
            return cls()
        f = cls()
        for fld in fields(cls):
            if fld.name not in data:
                continue
            value = data[fld.name]
            current = getattr(f, fld.name)
            if isinstance(current, bool):
                setattr(f, fld.name, bool(value))
            elif isinstance(current, list):
                setattr(f, fld.name, _str_list(value))
            else:
                hi = 1000 if fld.name.endswith("_rarity") else 100 if fld.name == "min_discount" else 10**12
                setattr(f, fld.name, _opt_int(value, 0, hi))
        f.events = [e for e in f.events if e in EVENTS] or ["upgrade"]
        return f

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Subscription:
    id: int
    chat_id: int  # где подписка настраивается
    name: str
    enabled: bool
    filters: Filters
    target_id: int | None = None  # куда слать (канал); None — в тот же чат

    @property
    def target(self) -> int:
        return self.target_id or self.chat_id
