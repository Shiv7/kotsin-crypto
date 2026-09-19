"""Position sizing from risk-per-trade, capped by leverage, guarded by liquidation distance."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .limits import RiskLimits


@dataclass(frozen=True, slots=True)
class SizingResult:
    contracts: int
    notional: float
    leverage: float
    risk_usd: float
    liq_distance_pct: float | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.contracts > 0


def size_position(
    *,
    balance: float,
    entry: float,
    stop: float,
    contract_value: float,
    maintenance_margin_pct: float,
    atr: float | None,
    limits: RiskLimits,
    open_notional: float = 0.0,
) -> SizingResult:
    dist = abs(entry - stop)
    if balance <= 0 or dist <= 0 or contract_value <= 0 or entry <= 0:
        return SizingResult(0, 0.0, 0.0, 0.0, None, "invalid inputs")
    risk_usd = balance * limits.risk_per_trade_pct / 100
    by_risk = math.floor(risk_usd / (dist * contract_value))
    room = balance * limits.max_leverage - open_notional
    by_lev = math.floor(room / (entry * contract_value)) if room > 0 else 0
    contracts = min(by_risk, by_lev)
    if contracts < 1:
        why = "risk budget < 1 contract" if by_risk < 1 else "leverage cap reached"
        return SizingResult(0, 0.0, 0.0, risk_usd, None, why)
    notional = contracts * contract_value * entry
    lev = (open_notional + notional) / balance
    # Cross margin: the whole balance backs the book. Distance to liquidation ≈ 1/lev − MM.
    liq_pct = (1 / lev - maintenance_margin_pct / 100) * 100 if lev > 0 else None
    if atr and liq_pct is not None and liq_pct < limits.min_liq_distance_atr * atr / entry * 100:
        return SizingResult(0, notional, lev, risk_usd, liq_pct, "liquidation too close")
    return SizingResult(contracts, notional, lev, risk_usd, liq_pct)
