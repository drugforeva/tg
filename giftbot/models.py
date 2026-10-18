from dataclasses import asdict, dataclass, field


@dataclass
class Upgrade:
    """Один только что улучшенный (сминченный) подарок."""

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

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class Filters:
    collections: list[str] = field(default_factory=list)  # пусто = все
    exclude_collections: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)  # названия, можно с *, пусто = любые
    backdrops: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    max_model_rarity: int | None = None  # промилле
    max_backdrop_rarity: int | None = None
    max_symbol_rarity: int | None = None
    min_one_in: int | None = None  # редкость сочетания не хуже «1 из N»
    max_num: int | None = None
    pretty_num: bool = False
    with_username: bool = False
    on_sale: bool = False
    max_price: int | None = None  # звёзды

    @classmethod
    def from_dict(cls, data: dict) -> "Filters":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Subscription:
    id: int
    chat_id: int
    name: str
    enabled: bool
    filters: Filters
