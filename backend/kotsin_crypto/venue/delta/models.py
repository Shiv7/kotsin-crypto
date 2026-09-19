"""Typed views of the Delta payloads we consume. Prices are Decimal; times are unix seconds (candles)
or microseconds (book/trades), exactly as the venue sends them — conversion happens in bars/."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class Candle(BaseModel, frozen=True):
    time: int  # unix seconds, bar start
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None  # None for MARK: / OI: / FUNDING: series


class Level(BaseModel, frozen=True):
    price: Decimal
    size: int


class Orderbook(BaseModel, frozen=True):
    symbol: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    last_updated_at: int  # microseconds

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Orderbook:
        return cls(
            symbol=raw["symbol"],
            bids=tuple(
                Level(price=Decimal(str(x["price"])), size=int(x["size"])) for x in raw["buy"]
            ),
            asks=tuple(
                Level(price=Decimal(str(x["price"])), size=int(x["size"])) for x in raw["sell"]
            ),
            last_updated_at=int(raw["last_updated_at"]),
        )

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None
