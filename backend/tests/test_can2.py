from __future__ import annotations

from kotsin_crypto.bars.unified import UnifiedBar
from kotsin_crypto.strategy.base import Side
from kotsin_crypto.strategy.can2 import Can2, Can2Config


class Ctx:
    def __init__(self, bars: list[UnifiedBar]) -> None:
        self._bars = bars
        self.state: dict[str, object] = {}

    def bars(self, symbol: str, tf: str, n: int) -> list[UnifiedBar]:
        return self._bars[-n:]


def _bar(
    i: int, o: float, h: float, lo: float, c: float, v: float, ts0: int = 1_700_000_000
) -> UnifiedBar:
    return UnifiedBar(
        symbol="BTCUSD",
        tf="5m",
        ts=ts0 + i * 300,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=v,
        has_trades=v > 0,
        trade_count=int(v),
    )


def _quiet(n: int) -> list[UnifiedBar]:
    return [_bar(i, 100.0, 100.5, 99.5, 100.0 + (i % 2) * 0.1, 100.0) for i in range(n)]


def test_long_on_surge_and_breakout() -> None:
    cfg = Can2Config(k_surge=2.0, median_window=10, n_lookback=6, vwap_window=6, atr_window=5)
    strat = Can2(cfg)
    bars = _quiet(cfg.min_history)
    bars.append(_bar(len(bars), 100.0, 103.0, 99.9, 102.5, 350.0))
    ctx = Ctx(bars)
    sigs = strat.on_bar(ctx, bars[-1])
    assert len(sigs) == 1
    s = sigs[0]
    assert s.side is Side.LONG and float(s.entry) == 102.5 and float(s.stop) < 102.5
    assert s.evidence["surge"] == 3.5
    assert [g.name for g in s.gates] == ["surge", "breakout", "vwap", "flow", "vpin"]
    assert all(g.passed for g in s.gates)
    assert all(g.missing for g in s.gates if g.name in ("flow", "vpin"))  # no tape → FAIL_OPEN
    assert len(s.signal_id) <= 32
    # cooldown: the very next bar cannot fire
    bars.append(_bar(len(bars), 102.5, 105.0, 102.0, 104.9, 400.0))
    assert (
        strat.on_bar(Ctx.__new__(Ctx), bars[-1]) == []
        if False
        else strat.on_bar(ctx, bars[-1]) == []
    )


def test_short_on_surge_and_breakdown() -> None:
    cfg = Can2Config(k_surge=2.0, median_window=10, n_lookback=6, vwap_window=6, atr_window=5)
    bars = _quiet(cfg.min_history)
    bars.append(_bar(len(bars), 100.0, 100.1, 97.0, 97.5, 300.0))
    sigs = Can2(cfg).on_bar(Ctx(bars), bars[-1])
    assert len(sigs) == 1 and sigs[0].side is Side.SHORT and float(sigs[0].stop) > 97.5


def test_no_signal_without_surge_or_breakout() -> None:
    cfg = Can2Config(k_surge=2.0, median_window=10, n_lookback=6, vwap_window=6, atr_window=5)
    bars = _quiet(cfg.min_history)
    bars.append(_bar(len(bars), 100.0, 103.0, 99.9, 102.5, 120.0))  # breakout, no surge
    assert Can2(cfg).on_bar(Ctx(bars), bars[-1]) == []
    bars[-1] = _bar(len(bars) - 1, 100.0, 100.4, 99.6, 100.2, 500.0)  # surge, no breakout
    assert Can2(cfg).on_bar(Ctx(bars), bars[-1]) == []


def test_insufficient_history_is_silent() -> None:
    cfg = Can2Config()
    bars = _quiet(5)
    assert Can2(cfg).on_bar(Ctx(bars), bars[-1]) == []
