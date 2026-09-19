"""Strategy contract. A strategy is a pure function of bars and its own persisted state:
no I/O, no wall clock, no venue, no orders (LEARNINGS R11; enforced by import-linter)."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from ..bars.unified import UnifiedBar
from .gates import GateResult
from .keys import StrategyKey


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class Signal:
    strategy: StrategyKey
    symbol: str
    side: Side
    ts: int  # decision bar start, unix seconds
    entry: Decimal
    stop: Decimal
    targets: tuple[Decimal, ...] = ()
    confidence: float = 1.0
    reason: str = ""
    gates: tuple[GateResult, ...] = ()
    evidence: Mapping[str, float] = field(default_factory=dict)

    @property
    def signal_id(self) -> str:
        # ≤ 32 chars so it can be the venue client_order_id verbatim.
        return f"{self.strategy.value}-{self.symbol}-{self.ts}-{self.side.value[0]}"[:32]


class Context(Protocol):
    def bars(self, symbol: str, tf: str, n: int) -> Sequence[UnifiedBar]: ...

    @property
    def state(self) -> MutableMapping[str, object]: ...


class Strategy(Protocol):
    key: StrategyKey
    timeframes: tuple[str, ...]

    def on_bar(self, ctx: Context, bar: UnifiedBar) -> list[Signal]: ...
