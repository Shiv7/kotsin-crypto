"""Gym-style environment for EXIT policies (the ratchet / time-stop replacement). The market
simulation is the backtester's: stops are checked against the next bar's open (gap) and extreme,
fees are charged on both legs, exits slip. The policy chooses among a small, inspectable action set
that only ever TIGHTENS the stop or exits, so the risk layer stays in charge.

Episode = one position from its fill bar to its close; step = one bar of the episode's timeframe;
observation = position state + market features computed from the bar window (the same function the
backtester uses, so live, backtest and this env see identical inputs); reward = change in net P&L
in units of R (initial risk), comparable across symbols and price levels.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

import numpy as np

from ..bars.unified import UnifiedBar

OBS_COLUMNS = [
    "r_now",
    "peak_r",
    "drawdown_from_peak_r",
    "bars_held",
    "hours_to_funding",
    "ret_3",
    "ret_12",
    "rv_12",
    "surge_20",
    "vwap_dist_12",
    "pos_in_range_48",
    "buy_ratio",
    "vpin_fast",
    "kyle_bps",
    "hour_sin",
    "hour_cos",
]
# Every action except hold/exit sets the stop to a level and only ever tightens it.
ACTIONS = (
    "hold",
    "lock_0r",
    "lock_1r",
    "lock_2r",
    "lock_3r",
    "trail_1_5r",
    "trail_1r",
    "trail_0_5r",
    "exit_now",
)
ACTION_INDEX = {a: i for i, a in enumerate(ACTIONS)}
FUNDING_INTERVAL_S = 8 * 3600


def market_features(window: Sequence[UnifiedBar]) -> dict[str, float]:
    """Point-in-time features from a bar window ending at the current bar (≤ 49 bars used)."""
    n = len(window)
    cur = window[-1]
    closes = [b.close for b in window]

    def lret(k: int) -> float:
        return math.log(closes[-1] / closes[-1 - k]) if n > k and closes[-1 - k] > 0 else 0.0

    rv = 0.0
    if n >= 13:
        lr = [math.log(closes[i] / closes[i - 1]) for i in range(n - 12, n) if closes[i - 1] > 0]
        if len(lr) >= 3:
            rv = float(np.std(lr)) * math.sqrt(12)
    vols = [b.volume for b in window[-21:-1]]
    med = median(vols) if len(vols) >= 5 else 0.0
    surge = cur.volume / med if med > 0 else 0.0
    w12 = window[-12:]
    vv = sum(b.volume for b in w12)
    vwap_dist = (cur.close / (sum(b.typical * b.volume for b in w12) / vv) - 1) if vv > 0 else 0.0
    w48 = window[-48:]
    hi, lo = max(b.high for b in w48), min(b.low for b in w48)
    pos = (cur.close - lo) / (hi - lo) if hi > lo else 0.5
    hour = (cur.ts % 86_400) / 3600
    return {
        "ret_3": lret(3),
        "ret_12": lret(12),
        "rv_12": rv,
        "surge_20": surge,
        "vwap_dist_12": vwap_dist,
        "pos_in_range_48": pos,
        "buy_ratio": (cur.buy_volume / cur.volume) if (cur.has_micro and cur.volume > 0) else 0.5,
        "vpin_fast": cur.vpin_fast if cur.vpin_fast is not None else 0.0,
        "kyle_bps": cur.kyle_lambda_15m_bps_per_1k
        if cur.kyle_lambda_15m_bps_per_1k is not None
        else 0.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "hours_to_funding": (
            (FUNDING_INTERVAL_S - (cur.end_ts % FUNDING_INTERVAL_S)) % FUNDING_INTERVAL_S
        )
        / 3600,
    }


def compute_obs(
    *,
    side: int,
    entry: float,
    r_unit: float,
    peak_r: float,
    bars_held: int,
    window: Sequence[UnifiedBar],
) -> np.ndarray:
    cur = window[-1]
    r_now = (cur.close - entry) * side / r_unit if r_unit > 0 else 0.0
    f = market_features(window)
    row = {
        "r_now": r_now,
        "peak_r": peak_r,
        "drawdown_from_peak_r": peak_r - r_now,
        "bars_held": float(bars_held),
        **f,
    }
    return np.array([row[c] for c in OBS_COLUMNS], dtype=float)


def stop_after_action(
    action: str, *, side: int, entry: float, r_unit: float, stop: float, peak_r: float
) -> float:
    """The stop implied by an action (tighten-only; ``hold``/``exit_now`` leave it unchanged)."""
    target: float | None = None
    if action.startswith("lock_"):
        k = float(action[5:-1].replace("_", "."))
        target = entry + side * k * r_unit
    elif action.startswith("trail_"):
        k = float(action[6:-1].replace("_", "."))
        target = entry + side * (peak_r - k) * r_unit
    if target is None:
        return stop
    return max(stop, target) if side > 0 else min(stop, target)


@dataclass(slots=True)
class ExitEpisode:
    symbol: str
    side: int  # +1 long, -1 short
    entry: float  # fill price (slippage already applied)
    r_unit: float  # |entry − initial stop|
    entry_index: int  # bar index of the fill (the policy first observes this bar's close)
    contract_value: float = 0.001
    fee_rate: float = 0.0005
    slip_bps: float = 1.0
    signal_ts: int = 0


@dataclass(slots=True)
class StepResult:
    obs: np.ndarray
    reward: float
    done: bool
    info: dict[str, Any] = field(default_factory=dict)


class ExitEnv:
    """Deterministic given (bars, episode, actions)."""

    def __init__(
        self,
        bars: Sequence[UnifiedBar],
        episode: ExitEpisode,
        *,
        max_bars: int = 48,
        window: int = 49,
    ) -> None:
        self.bars = bars
        self.ep = episode
        self.max_bars = max_bars
        self.window = window
        self.i = episode.entry_index
        self.stop = episode.entry - episode.side * episode.r_unit
        self.peak_r = 0.0
        self.done = False
        self.exit_price: float | None = None
        self.exit_reason = ""
        self.fees_r = 0.0
        self._last_net_r = self._net_r(bars[self.i].close)

    # ---- accounting ---------------------------------------------------------------------------
    def _r(self, px: float) -> float:
        return (px - self.ep.entry) * self.ep.side / self.ep.r_unit

    def _fees_r(self, exit_px: float) -> float:
        return (self.ep.entry + exit_px) * self.ep.fee_rate / self.ep.r_unit

    def _net_r(self, px: float) -> float:
        return self._r(px) - self._fees_r(px)

    def obs(self) -> np.ndarray:
        lo = max(0, self.i - self.window + 1)
        return compute_obs(
            side=self.ep.side,
            entry=self.ep.entry,
            r_unit=self.ep.r_unit,
            peak_r=self.peak_r,
            bars_held=self.i - self.ep.entry_index,
            window=self.bars[lo : self.i + 1],
        )

    def reset(self) -> np.ndarray:
        return self.obs()

    # ---- dynamics -------------------------------------------------------------------------------
    def step(self, action: int) -> StepResult:
        if self.done:
            raise RuntimeError("episode finished")
        act = ACTIONS[action]
        b = self.bars[self.i]
        side = self.ep.side
        if act == "exit_now":
            return self._finish(b.close, "policy_exit")
        self.stop = stop_after_action(
            act,
            side=side,
            entry=self.ep.entry,
            r_unit=self.ep.r_unit,
            stop=self.stop,
            peak_r=self.peak_r,
        )
        self.i += 1
        if self.i >= len(self.bars):
            self.i = len(self.bars) - 1
            return self._finish(self.bars[-1].close, "data_end")
        nb = self.bars[self.i]
        if (side > 0 and nb.open <= self.stop) or (side < 0 and nb.open >= self.stop):
            return self._finish(nb.open, "stop_gap")
        adverse = nb.low if side > 0 else nb.high
        if (side > 0 and adverse <= self.stop) or (side < 0 and adverse >= self.stop):
            return self._finish(self.stop, "stop")
        favourable = nb.high if side > 0 else nb.low
        self.peak_r = max(self.peak_r, self._r(favourable))
        if self.i - self.ep.entry_index >= self.max_bars:
            return self._finish(nb.close, "time_stop")
        net = self._net_r(nb.close)
        reward = net - self._last_net_r
        self._last_net_r = net
        return StepResult(self.obs(), reward, False, {"r_now": self._r(nb.close)})

    def _finish(self, px: float, reason: str) -> StepResult:
        fill = px - self.ep.side * px * self.ep.slip_bps / 1e4
        self.done = True
        self.exit_price = fill
        self.exit_reason = reason
        self.fees_r = self._fees_r(fill)
        net = self._net_r(fill)
        reward = net - self._last_net_r
        self._last_net_r = net
        return StepResult(
            self.obs(),
            reward,
            True,
            {
                "exit": fill,
                "reason": reason,
                "net_r": net,
                "bars_held": self.i - self.ep.entry_index,
            },
        )

    @property
    def net_r(self) -> float:
        return self._last_net_r

    @property
    def bars_held(self) -> int:
        return self.i - self.ep.entry_index
