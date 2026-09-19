from __future__ import annotations

from kotsin_crypto.bars.unified import UnifiedBar
from kotsin_crypto.research.backtest import BacktestConfig, BacktestRunner, ProductSpec

T0 = 1_780_000_000 - 1_780_000_000 % 28_800  # aligned to a funding boundary


def _bars(
    symbol: str, n_min: int, event_at: int | None = None, drift: float = 0.0
) -> list[UnifiedBar]:
    out = []
    px = 100.0
    for i in range(n_min):
        ts = T0 + i * 60
        vol = 20.0
        o = px
        h, lo = px + 0.05, px - 0.05
        c = px + drift
        if event_at is not None and event_at <= i < event_at + 5:
            vol = 400.0  # surge across one 5m bucket
            c = px + 0.6
            h = c + 0.1
        px = c
        out.append(
            UnifiedBar(
                symbol=symbol,
                tf="1m",
                ts=ts,
                open=o,
                high=max(h, o, c),
                low=min(lo, o, c),
                close=c,
                volume=vol,
                has_trades=True,
                trade_count=int(vol),
                source="rest",
            )
        )
    return out


PRODUCTS = {"BTCUSD": ProductSpec(contract_value=0.001, maintenance_margin_pct=0.25)}
PARAMS = {
    "k_surge": 2.0,
    "median_window": 10,
    "n_lookback": 6,
    "vwap_window": 6,
    "atr_window": 5,
    "cooldown_bars": 2,
}


def _run(bars: list[UnifiedBar], **cfg_over: object) -> dict:
    cfg = BacktestConfig(
        symbols=("BTCUSD",), start=bars[0].ts, end=bars[-1].ts + 60, params=PARAMS, **cfg_over
    )  # type: ignore[arg-type]
    return BacktestRunner(cfg, PRODUCTS, {"BTCUSD": [(T0 - 3600, 0.01)]}).run({"BTCUSD": bars})


def test_no_signals_means_no_trades_and_no_costs() -> None:
    r = _run(_bars("BTCUSD", 600))
    assert (
        r["stats"]["trades"] == 0
        and r["stats"]["net"] == 0
        and r["costs"] == {"fees": 0.0, "funding": 0.0, "slippage": 0.0}
    )
    assert r["stats"]["final_balance"] == 10_000.0 and r["stats"]["bars_1m"] == 600


def test_surge_breakout_opens_next_bar_and_costs_are_applied() -> None:
    bars = _bars("BTCUSD", 900, event_at=300)
    r = _run(bars)
    s = r["stats"]
    assert s["signals"] >= 1 and s["trades"] == 1
    t = r["trades"][0]
    assert t["side"] == "LONG"
    decision_close = next(
        b for b in bars if b.ts == T0 + 304 * 60
    )  # last minute of the surge bucket
    fill_bar = next(b for b in bars if b.ts == decision_close.ts + 60)
    assert abs(t["entry"] - fill_bar.open * (1 + 0.5 / 1e4)) < 1e-9  # next-bar open + 0.5 bps
    assert t["fees"] > 0 and r["costs"]["slippage"] > 0
    assert abs(t["net"] - (t["pnl"] - t["fees"] - t["funding"])) < 1e-9
    assert t["exit_reason"] in ("STOP", "TIME_STOP", "END")
    assert abs(s["final_balance"] - (10_000 + s["gross"] - s["fees"] - s["funding"])) < 1e-6


def test_funding_is_charged_to_open_positions_at_boundaries() -> None:
    bars = _bars("BTCUSD", 28_800 // 60 + 400, event_at=300, drift=0.0)
    cfg_limits = {"time_stop_s": 10 * 24 * 3600}  # keep it open across the 8h boundary
    r = _run(bars, limits=cfg_limits)
    assert r["stats"]["trades"] == 1
    assert r["costs"]["funding"] > 0 and r["trades"][0]["funding"] == r["costs"]["funding"]
    r2 = _run(bars, limits=cfg_limits, apply_funding=False)
    assert r2["costs"]["funding"] == 0.0


def test_deterministic() -> None:
    bars = _bars("BTCUSD", 900, event_at=300)
    a, b = _run(bars), _run(bars)
    strip = lambda tr: [{k: v for k, v in t.items() if k not in ("id", "position_id")} for t in tr]  # noqa: E731
    assert strip(a["trades"]) == strip(b["trades"]) and a["stats"] == b["stats"]
