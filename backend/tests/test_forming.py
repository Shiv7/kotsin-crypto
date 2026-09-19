from __future__ import annotations

from kotsin_crypto.bars.trade_bar import TradeBarBuilder
from kotsin_crypto.bars.unified import BarStore, merge_1m

M = 60_000_000


def test_forming_bar_aggregates_partial_bucket_and_current_minute() -> None:
    store = BarStore(["BTCUSD"], tfs=("1m", "5m"))
    base = 6_000_000 * 60 - (6_000_000 * 60) % 300
    tb = TradeBarBuilder("BTCUSD")
    # two closed minutes in the bucket
    for i in range(2):
        tb.on_trade((base + i * 60) * M // 60 + 1_000, 100.0 + i, 5, True)
        for bar in tb.flush(base + i * 60 + 61.5):
            store.add_1m(merge_1m(bar, None))
    # third minute forming
    tb.on_trade((base + 120) * M // 60 + 1_000, 105.0, 7, False)
    cur = tb.current()
    assert cur is not None and cur.volume == 7 and cur.close == 105.0
    f = store.forming("BTCUSD", "5m", merge_1m(cur, None, source="forming"))
    assert f is not None and f.tf == "5m" and f.ts == base and f.source == "forming"
    assert (f.open, f.high, f.low, f.close, f.volume) == (100.0, 105.0, 100.0, 105.0, 17.0)
    assert store.forming("BTCUSD", "1m", merge_1m(cur, None)).ts == base + 120
    assert store.forming("BTCUSD", "5m", None) is not None  # partial only, no forming minute
    assert BarStore(["X"]).forming("X", "5m", None) is None
