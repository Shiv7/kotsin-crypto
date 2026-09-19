"""Per-strategy paper wallet: balance, day/peak accounting, breakers. Persisted through the ledger;
the same object drives PAPER and (later) LIVE accounting."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .limits import RiskLimits


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


@dataclass(slots=True)
class Wallet:
    strategy: str
    initial: float
    balance: float
    peak: float
    day_start_balance: float
    day: str
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    updated_ts: float = field(default_factory=time.time)

    @classmethod
    def new(cls, strategy: str, initial: float, now: float | None = None) -> Wallet:
        now = now or time.time()
        return cls(
            strategy=strategy,
            initial=initial,
            balance=initial,
            peak=initial,
            day_start_balance=initial,
            day=utc_day(now),
            updated_ts=now,
        )

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Wallet:
        return cls(**d)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def day_pnl(self) -> float:
        return self.balance - self.day_start_balance

    @property
    def day_pnl_pct(self) -> float:
        return self.day_pnl / self.day_start_balance * 100 if self.day_start_balance else 0.0

    @property
    def drawdown_pct(self) -> float:
        return (self.peak - self.balance) / self.peak * 100 if self.peak else 0.0

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None

    def rollover(self, now: float) -> bool:
        """Start a new UTC day: reset the day baseline and lift a daily-loss halt."""
        d = utc_day(now)
        if d == self.day:
            return False
        self.day = d
        self.day_start_balance = self.balance
        if self.halted and self.halt_reason.startswith("DAILY_LOSS"):
            self.halted, self.halt_reason = False, ""
        self.updated_ts = now
        return True

    def apply_fee(self, fee: float, now: float) -> None:
        self.balance -= fee
        self.fees_paid += fee
        self.updated_ts = now

    def apply_funding(self, amount: float, now: float) -> None:
        """``amount`` > 0 means paid."""
        self.balance -= amount
        self.funding_paid += amount
        self.updated_ts = now

    def apply_close(self, pnl: float, now: float) -> None:
        self.balance += pnl
        self.realized_pnl += pnl
        self.trades += 1
        if pnl > 0:
            self.wins += 1
        elif pnl < 0:
            self.losses += 1
        self.peak = max(self.peak, self.balance)
        self.updated_ts = now

    def check_breakers(self, limits: RiskLimits, now: float) -> str | None:
        """Trip a halt if a limit is breached; returns the reason when newly tripped."""
        if self.halted:
            return None
        if self.day_pnl_pct <= -limits.daily_loss_limit_pct:
            self.halted, self.halt_reason = True, f"DAILY_LOSS {self.day_pnl_pct:.2f}%"
        elif self.drawdown_pct >= limits.max_drawdown_pct:
            self.halted, self.halt_reason = True, f"DRAWDOWN {self.drawdown_pct:.2f}%"
        else:
            return None
        self.updated_ts = now
        return self.halt_reason
