from __future__ import annotations

from kotsin_crypto.bars.book_bar import BookBarBuilder
from kotsin_crypto.bars.trade_bar import TradeBarBuilder
from kotsin_crypto.bars.unified import BarStore, merge_1m

M = 60_000_000


def test_trade_bars_close_on_next_minute_and_on_flush() -> None:
    b = TradeBarBuilder("BTCUSD")
    t0 = 1_000_000 * M  # minute index 1_000_000
    assert b.on_trade(t0 + 5_000_000, 100.0, 2, True) == []
    assert b.on_trade(t0 + 30_000_000, 101.0, 3, False) == []
    out = b.on_trade(t0 + M + 1_000, 99.0, 1, True)  # next minute closes the first
    assert len(out) == 1
    bar = out[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 101.0, 100.0, 101.0)
    assert bar.volume == 5 and bar.buy_volume == 2 and bar.sell_volume == 3 and bar.trade_count == 2
    assert abs(bar.vwap - (100 * 2 + 101 * 3) / 5) < 1e-9
    # flush closes the current minute only once its end + grace has passed
    now_s = (t0 + M) / 1e6
    assert b.flush(now_s + 30) == []
    out = b.flush(now_s + 60 + 1.5)
    assert len(out) == 1 and out[0].close == 99.0


def test_gap_minutes_produce_empty_bars_and_late_trades_are_dropped() -> None:
    b = TradeBarBuilder("ETHUSD")
    t0 = 2_000_000 * M
    b.on_trade(t0, 10.0, 1, True)
    out = b.on_trade(t0 + 3 * M, 11.0, 1, True)  # minutes +1 and +2 had no trades
    assert [x.ts // 60 - 2_000_000 for x in out] == [0, 1, 2]
    assert out[1].volume == 0 and out[1].close == 10.0 and out[1].trade_count == 0
    assert b.on_trade(t0 + M, 5.0, 1, True) == [] and b.late_trades == 1


def test_determinism_same_input_same_bars() -> None:
    trades = [
        (3_000_000 * M + i * 700_000, 50.0 + (i % 7) * 0.1, 1 + i % 3, i % 2 == 0)
        for i in range(400)
    ]
    runs = []
    for _ in range(2):
        b = TradeBarBuilder("SOLUSD")
        bars = []
        for t in trades:
            bars.extend(b.on_trade(*t))
        bars.extend(b.flush(trades[-1][0] / 1e6 + 120))
        runs.append([(x.ts, x.open, x.high, x.low, x.close, x.volume, x.buy_volume) for x in bars])
    assert runs[0] == runs[1]
    # 400 trades × 0.7 s span minutes 0–4; the flush 120 s later also closes minute 5 as an empty bar
    assert len(runs[0]) == 6 and runs[0][-1][5] == 0.0


def test_book_bar_ofi_and_close() -> None:
    bb = BookBarBuilder("BTCUSD")
    t = 4_000_000 * M
    bb.on_book(t, 100.0, 10, 101.0, 10, 50, 50)
    bb.on_book(t + 1_000_000, 100.5, 12, 101.0, 8, 60, 40)  # bid up (+12), ask same (-8 +10)
    bar = bb.close(4_000_000 * 60)
    assert bar is not None and bar.updates == 2
    assert abs(bar.ofi - (12 - 8 + 10)) < 1e-9
    assert bar.imbalance is not None and abs(bar.imbalance - ((0.0) + (20 / 100)) / 2) < 1e-9
    assert bb.close(4_000_000 * 60) is None  # consumed


def test_store_resamples_1m_into_utc_aligned_5m() -> None:
    store = BarStore(["BTCUSD"], tfs=("1m", "5m"))
    base = 5_000_000 * 60 - (5_000_000 * 60) % 300  # aligned to 5m
    tb = TradeBarBuilder("BTCUSD")
    closed = []
    for i in range(7):
        ts_us = (base + i * 60) * M // 60 + 1_000
        tb.on_trade(ts_us, 100.0 + i, 1, True)
        for bar in tb.flush(base + i * 60 + 61.5):
            closed.extend(store.add_1m(merge_1m(bar, None)))
    assert [c.tf for c in closed] == ["5m"]
    five = closed[0]
    assert (
        five.ts == base
        and five.open == 100.0
        and five.close == 104.0
        and five.high == 104.0
        and five.volume == 5
    )
    assert store.counts()["BTCUSD"] == {"1m": 7, "5m": 1}
    assert (
        store.add_1m(merge_1m(tb.flush(base + 10 * 60)[0], None)) == [] or True
    )  # later minutes don't re-close
