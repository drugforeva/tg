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
    resale_stars: int | None = None
    detected_at: int = 0

    @property
    def link(self) -> str:
        return f"https://t.me/nft/{self.slug}"

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class Filters:
    collections: list[str] = field(default_factory=list)  # пусто = все
    models: list[str] = field(default_factory=list)  # подстроки, пусто = любые
    backdrops: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    max_model_rarity: int | None = None  # промилле
    max_backdrop_rarity: int | None = None
    max_symbol_rarity: int | None = None
    max_num: int | None = None
    pretty_num: bool = False
    with_username: bool = False
    on_sale: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "Filters":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)
