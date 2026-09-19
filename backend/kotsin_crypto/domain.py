"""Core records shared by risk / exec / ledger / api. Strategies never import this — they only emit
``strategy.base.Signal``; everything from sizing onwards speaks these types."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class PosSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Purpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class ExitReason(StrEnum):
    STOP = "STOP"
    TIME_STOP = "TIME_STOP"
    HALT = "HALT"
    DAILY_LOSS = "DAILY_LOSS"
    MANUAL = "MANUAL"
    END = "END"  # backtest range ended with the position open


@dataclass(slots=True)
class Level:
    """One price level of an order book (also used by the tape-side microstructure builder)."""

    price: float
    size: int


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def to_json(obj: Any) -> dict[str, Any]:
    return asdict(obj)


@dataclass(slots=True)
class OrderIntent:
    strategy: str
    symbol: str
    side: OrderSide
    contracts: int
    purpose: Purpose
    signal_id: str
    client_order_id: str  # idempotency key; == signal_id for entries
    reason: str = ""
    position_id: str | None = None
    ref_price: float | None = None  # price the decision was made at
    ts: float = field(default_factory=time.time)


@dataclass(slots=True)
class Fill:
    price: float
    contracts: int
    ts: float
    fee: float
    slippage_bps: float | None = None
    book_age_ms: int | None = None
    levels: int = 0


@dataclass(slots=True)
class Order:
    id: str
    client_order_id: str
    strategy: str
    symbol: str
    side: OrderSide
    purpose: Purpose
    contracts: int
    mode: str
    status: str  # FILLED | SHADOW | REJECTED
    signal_id: str
    position_id: str | None
    reason: str
    ts: float
    avg_price: float | None = None
    filled: int = 0
    fee: float = 0.0
    slippage_bps: float | None = None
    note: str = ""


@dataclass(slots=True)
class Position:
    id: str
    strategy: str
    symbol: str
    side: PosSide
    contracts: int
    entry: float
    stop: float
    initial_stop: float
    opened_ts: float
    signal_id: str
    contract_value: float
    r_unit: float  # |entry - initial_stop| in price units; the denominator of every R figure
    notional: float = 0.0
    leverage: float = 0.0
    peak_r: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    status: str = "OPEN"
    closed_ts: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    bars_held: int = 0

    @property
    def direction(self) -> int:
        return 1 if self.side is PosSide.LONG else -1

    def unrealized(self, mark: float) -> float:
        return (mark - self.entry) * self.direction * self.contracts * self.contract_value

    def r_now(self, mark: float) -> float:
        return (mark - self.entry) * self.direction / self.r_unit if self.r_unit > 0 else 0.0


@dataclass(slots=True)
class Trade:
    id: str
    position_id: str
    strategy: str
    symbol: str
    side: PosSide
    contracts: int
    entry: float
    exit: float
    pnl: float  # gross, in settling asset
    fees: float
    funding: float
    net: float
    r_multiple: float
    mfe_r: float
    mae_r: float
    exit_reason: str
    opened_ts: float
    closed_ts: float
    duration_s: float
    signal_id: str


@dataclass(slots=True)
class ExitDecision:
    position_id: str
    reason: ExitReason
    ref_price: float
    note: str = ""
