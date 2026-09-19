"""Gym-style environment over the backtester, for policies that decide EXITS (the ratchet/time-stop
replacement) or ENTRY sizing. The market simulation is exactly the backtester's (next-bar fills,
intrabar stops, fees, funding, slippage); the policy only chooses among a small action set, so the
learned object stays inspectable and the risk layer stays in charge.

Episode = one open position from its fill to its close; step = one 5m bar; observation = position
state + market features; reward = change in net P&L per step in units of R (initial risk), so that
rewards are comparable across symbols and price levels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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
ACTIONS = ("hold", "stop_to_breakeven", "trail_1r", "trail_0_5r", "exit_now")


@dataclass(slots=True)
class ExitEpisode:
    symbol: str
    side: int  # +1 long, -1 short
    entry: float
    r_unit: float
    entry_index: int  # index into bars/features where the fill happened (fill at this bar's open)
    contract_value: float
    fee_rate: float = 0.0005
    slip_bps: float = 1.0


@dataclass(slots=True)
class StepResult:
    obs: np.ndarray
    reward: float
    done: bool
    info: dict[str, Any] = field(default_factory=dict)


class ExitEnv:
    """Deterministic given (bars, features, episode, actions). ``reset`` returns the first observation
    at the fill bar's close; each ``step`` advances one bar."""

    def __init__(
        self,
        bars: Sequence[UnifiedBar],
        features: dict[str, np.ndarray],
        episode: ExitEpisode,
        max_bars: int = 48,
    ) -> None:
        self.bars = bars
        self.f = features
        self.ep = episode
        self.max_bars = max_bars
        self.i = episode.entry_index
        self.stop = episode.entry - episode.side * episode.r_unit
        self.peak_r = 0.0
        self.done = False
        self.exit_price: float | None = None
        self.exit_reason = ""
        self._last_net_r = self._net_r(bars[self.i].close)

    def _r(self, px: float) -> float:
        return (px - self.ep.entry) * self.ep.side / self.ep.r_unit

    def _net_r(self, px: float) -> float:
        fees = (self.ep.entry + px) * self.ep.fee_rate  # both legs, per contract-unit
        return self._r(px) - fees / self.ep.r_unit

    def obs(self) -> np.ndarray:
        b = self.bars[self.i]
        r_now = self._r(b.close)
        hours_to_funding = ((28_800 - (b.end_ts % 28_800)) % 28_800) / 3600
        row = {
            "r_now": r_now,
            "peak_r": self.peak_r,
            "drawdown_from_peak_r": self.peak_r - r_now,
            "bars_held": float(self.i - self.ep.entry_index),
            "hours_to_funding": hours_to_funding,
            "hour_sin": self.f["hour_sin"][self.i],
            "hour_cos": self.f["hour_cos"][self.i],
        }
        for c in (
            "ret_3",
            "ret_12",
            "rv_12",
            "surge_20",
            "vwap_dist_12",
            "pos_in_range_48",
            "buy_ratio",
            "vpin_fast",
            "kyle_bps",
        ):
            v = self.f[c][self.i]
            row[c] = 0.0 if np.isnan(v) else float(v)
        return np.array([row[c] for c in OBS_COLUMNS], dtype=float)

    def reset(self) -> np.ndarray:
        return self.obs()

    def step(self, action: int) -> StepResult:
        if self.done:
            raise RuntimeError("episode finished")
        act = ACTIONS[action]
        b = self.bars[self.i]
        side = self.ep.side
        # 1. policy adjusts the stop (never loosens it)
        if act == "stop_to_breakeven":
            self._tighten(self.ep.entry)
        elif act == "trail_1r":
            self._tighten(self.ep.entry + side * (self.peak_r - 1.0) * self.ep.r_unit)
        elif act == "trail_0_5r":
            self._tighten(self.ep.entry + side * (self.peak_r - 0.5) * self.ep.r_unit)
        elif act == "exit_now":
            return self._finish(b.close, "policy_exit")
        # 2. advance one bar; stop checked against the extreme, then peak updated
        self.i += 1
        if self.i >= len(self.bars):
            return self._finish(self.bars[-1].close, "data_end")
        nb = self.bars[self.i]
        adverse = nb.low if side > 0 else nb.high
        if (side > 0 and nb.open <= self.stop) or (side < 0 and nb.open >= self.stop):
            return self._finish(nb.open, "stop_gap")
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

    def _tighten(self, new_stop: float) -> None:
        if (self.ep.side > 0 and new_stop > self.stop) or (
            self.ep.side < 0 and new_stop < self.stop
        ):
            self.stop = new_stop

    def _finish(self, px: float, reason: str) -> StepResult:
        slip = px * self.ep.slip_bps / 1e4
        fill = px - self.ep.side * slip
        self.done = True
        self.exit_price = fill
        self.exit_reason = reason
        net = self._net_r(fill)
        reward = net - self._last_net_r
        self._last_net_r = net
        return StepResult(
            self.obs() if self.i < len(self.bars) else np.zeros(len(OBS_COLUMNS)),
            reward,
            True,
            {"exit": fill, "reason": reason, "net_r": net},
        )

    @property
    def net_r(self) -> float:
        return self._last_net_r
