"""CAN2-crypto — 5m volume-surge + breakout momentum, both directions.

Re-implemented for a 24/7 perpetual: no session VWAP (a rolling VWAP over the lookback instead), no
prior-day close, UTC buckets. Gates declare what missing data means (R5). Pure: bars in, signals out.

Parameters are a frozen config so every value has one home and shows up in the strategy doc.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from ..bars.unified import TF_SECONDS, UnifiedBar
from .base import Context, Side, Signal
from .gates import Gate, GateResult, OnMissing, chain_passed
from .keys import StrategyKey


@dataclass(frozen=True, slots=True)
class Can2Config:
    tf: str = "5m"
    k_surge: float = 2.5  # bar volume / median of the previous `median_window` bars
    median_window: int = 20
    n_lookback: int = 12  # breakout above the max high (below the min low) of the previous N bars
    vwap_window: int = 12
    atr_window: int = 14
    sl_atr_mult: float = 1.5
    sl_floor_pct: float = 0.15  # never place the stop closer than this % of price
    cooldown_bars: int = 6
    allow_short: bool = True

    @property
    def min_history(self) -> int:
        return max(self.median_window, self.n_lookback, self.vwap_window, self.atr_window) + 2


class Can2:
    key = StrategyKey.CAN2
    timeframes = ("5m",)

    def __init__(self, cfg: Can2Config | None = None) -> None:
        self.cfg = cfg or Can2Config()
        self.timeframes = (self.cfg.tf,)
        self.g_surge = Gate("surge", OnMissing.FAIL_CLOSED)
        self.g_break = Gate("breakout", OnMissing.FAIL_CLOSED)
        self.g_vwap = Gate("vwap", OnMissing.FAIL_OPEN)

    def on_bar(self, ctx: Context, bar: UnifiedBar) -> list[Signal]:
        cfg = self.cfg
        if bar.tf != cfg.tf or not bar.has_trades:
            return []
        hist = ctx.bars(bar.symbol, cfg.tf, cfg.min_history + 1)
        if len(hist) < cfg.min_history or hist[-1].ts != bar.ts:
            return []
        prev = hist[:-1]

        last_ts = ctx.state.get(f"last_signal_ts:{bar.symbol}")
        if (
            isinstance(last_ts, int | float)
            and (bar.ts - last_ts) < cfg.cooldown_bars * TF_SECONDS[cfg.tf]
        ):
            return []

        vols = [b.volume for b in prev[-cfg.median_window :]]
        med = median(vols) if vols else 0.0
        surge = bar.volume / med if med > 0 else None
        look = prev[-cfg.n_lookback :]
        hh = max(b.high for b in look)
        ll = min(b.low for b in look)
        vwap = _rolling_vwap([*prev[-cfg.vwap_window :], bar])
        atr = _atr(hist, cfg.atr_window)
        if atr is None or atr <= 0:
            return []

        evidence = {
            "surge": surge or 0.0,
            "median_vol": med,
            "hh": hh,
            "ll": ll,
            "vwap": vwap or 0.0,
            "atr": atr,
            "atr_pct": atr / bar.close * 100,
        }
        k = cfg.k_surge
        long_gates = (
            self.g_surge.evaluate(surge, lambda v: v >= k, threshold=k),
            self.g_break.evaluate(bar.close, lambda v: v > hh, threshold=hh),
            self.g_vwap.evaluate(vwap, lambda v: bar.close > v, threshold=vwap),
        )
        if chain_passed(long_gates):
            return [self._signal(ctx, bar, Side.LONG, long_gates, evidence, atr)]
        if cfg.allow_short:
            short_gates = (
                self.g_surge.evaluate(surge, lambda v: v >= k, threshold=k),
                self.g_break.evaluate(bar.close, lambda v: v < ll, threshold=ll),
                self.g_vwap.evaluate(vwap, lambda v: bar.close < v, threshold=vwap),
            )
            if chain_passed(short_gates):
                return [self._signal(ctx, bar, Side.SHORT, short_gates, evidence, atr)]
        return []

    def _signal(
        self,
        ctx: Context,
        bar: UnifiedBar,
        side: Side,
        gates: tuple[GateResult, ...],
        evidence: dict[str, float],
        atr: float,
    ) -> Signal:
        cfg = self.cfg
        floor = bar.close * cfg.sl_floor_pct / 100
        dist = max(cfg.sl_atr_mult * atr, floor)
        if side is Side.LONG:
            stop = min(bar.low, bar.close - dist)
        else:
            stop = max(bar.high, bar.close + dist)
        ctx.state[f"last_signal_ts:{bar.symbol}"] = bar.ts
        return Signal(
            strategy=self.key,
            symbol=bar.symbol,
            side=side,
            ts=bar.ts,
            entry=Decimal(str(bar.close)),
            stop=Decimal(str(round(stop, 8))),
            confidence=min(1.0, (evidence["surge"] / cfg.k_surge) / 2),
            reason=f"surge {evidence['surge']:.1f}x, {'breakout' if side is Side.LONG else 'breakdown'} of {cfg.n_lookback}-bar range",
            gates=gates,
            evidence=evidence,
        )


def _rolling_vwap(bars: list[UnifiedBar]) -> float | None:
    vol = sum(b.volume for b in bars)
    if vol <= 0:
        return None
    return sum(b.typical * b.volume for b in bars) / vol


def _atr(bars: list[UnifiedBar], window: int) -> float | None:
    if len(bars) < window + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-window - 1 : -1], bars[-window:], strict=True):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / len(trs) if trs else None
