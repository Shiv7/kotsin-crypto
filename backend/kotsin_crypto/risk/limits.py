"""Risk limits — one frozen record, one home (R1)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskLimits:
    risk_per_trade_pct: float = 0.5  # of wallet balance, lost if the initial stop is hit
    max_leverage: float = 3.0  # notional / balance, per position AND summed per wallet
    max_positions_total: int = 3
    max_positions_per_symbol: int = 1
    daily_loss_limit_pct: float = (
        2.0  # of the day-start balance → entries halted until next UTC day
    )
    max_drawdown_pct: float = 8.0  # from peak → wallet halted
    min_liq_distance_atr: float = 3.0  # liquidation must be at least this many ATRs away
    time_stop_s: int = 4 * 3600
    ratchet: tuple[tuple[float, float], ...] = ((1.0, 0.0), (2.0, 1.0), (3.0, 2.0), (4.0, 3.0))
    trail_r_after_ladder: float = 1.5
