"""L2 book state per symbol, rebuilt from full snapshots (``ob_l2``). Floats on purpose: this is the
hot path; Decimal appears only at the order boundary. Exposes what the paper matcher and the book-bar
builder need: best quotes, mid/microprice, depth, imbalance, and a ladder walk for a market order."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Level:
    price: float
    size: int


@dataclass(slots=True)
class WalkResult:
    requested: int
    filled: int
    avg_price: float
    worst_price: float
    levels: int

    @property
    def complete(self) -> bool:
        return self.filled == self.requested


class Book:
    __slots__ = ("asks", "bids", "seq", "symbol", "ts_us", "updates")

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: list[Level] = []  # best (highest) first
        self.asks: list[Level] = []  # best (lowest) first
        self.ts_us = 0
        self.seq: int | None = None
        self.updates = 0

    def replace(
        self, bids: list[Level], asks: list[Level], ts_us: int, seq: int | None = None
    ) -> None:
        self.bids = sorted(bids, key=lambda lv: -lv.price)
        self.asks = sorted(asks, key=lambda lv: lv.price)
        self.ts_us = ts_us
        self.seq = seq
        self.updates += 1

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread_bps(self) -> float | None:
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return (self.asks[0].price - self.bids[0].price) / mid * 1e4

    @property
    def microprice(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        b, a = self.bids[0], self.asks[0]
        tot = b.size + a.size
        return (b.price * a.size + a.price * b.size) / tot if tot else self.mid

    def depth(self, side: str, n: int = 5) -> int:
        levels = self.bids if side == "bid" else self.asks
        return sum(lv.size for lv in levels[:n])

    def imbalance(self, n: int = 5) -> float | None:
        b, a = self.depth("bid", n), self.depth("ask", n)
        return (b - a) / (b + a) if (b + a) else None

    def age_ms(self, now_us: int) -> int | None:
        return (now_us - self.ts_us) // 1000 if self.ts_us else None

    def walk(self, side: str, contracts: int) -> WalkResult:
        """Simulate a market order: BUY consumes asks, SELL consumes bids."""
        levels = self.asks if side == "BUY" else self.bids
        remaining = contracts
        cost = 0.0
        worst = 0.0
        used = 0
        for lv in levels:
            if remaining <= 0:
                break
            take = min(lv.size, remaining)
            cost += take * lv.price
            worst = lv.price
            remaining -= take
            used += 1
        filled = contracts - remaining
        avg = cost / filled if filled else 0.0
        return WalkResult(
            requested=contracts, filled=filled, avg_price=avg, worst_price=worst, levels=used
        )
