"""UnifiedBar — the one record strategies see. Trade, book, OI, funding and mark data are merged at
query time with explicit ``has_*`` flags (never silently zero). Windows are UTC-aligned.

Step 3 fills in the builders; the dataclass is fixed now because strategy/ programs against it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UnifiedBar:
    symbol: str
    tf: str  # "1m" | "5m" | "30m" ...
    ts: int  # bar START, unix seconds, UTC-aligned

    open: float
    high: float
    low: float
    close: float
    volume: float  # contracts
    has_trades: bool = False
    buy_volume: float = 0.0  # taker-buy contracts
    sell_volume: float = 0.0  # taker-sell contracts
    trade_count: int = 0
    vwap: float | None = None
    vpin: float | None = None
    poc: float | None = None
    vah: float | None = None
    val: float | None = None

    has_book: bool = False
    ofi: float | None = None
    kyle_lambda: float | None = None
    microprice: float | None = None
    spread: float | None = None
    depth_imbalance: float | None = None
    book_staleness_ms: int | None = None

    has_oi: bool = False
    oi: float | None = None

    has_funding: bool = False
    funding_rate: float | None = None

    has_mark: bool = False
    mark_close: float | None = None
