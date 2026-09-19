from __future__ import annotations

import math
import random

from kotsin_crypto.bars.micro import MicroBuilder, VpinTracker, ols_origin
from kotsin_crypto.feed.book import Level

M = 60_000_000
S = 1_000_000


def test_ols_origin_recovers_slope() -> None:
    pairs = [(2.0 * q, q) for q in (-5.0, 3.0, 8.0, -1.0)]
    lam, r2 = ols_origin(pairs)
    assert lam == 2.0 and r2 == 1.0
    assert ols_origin([(1.0, 1.0)]) == (None, None)


def test_vpin_extremes() -> None:
    v = VpinTracker(bucket_volume=100, n_buckets=5)
    for _ in range(5):
        v.add(100, True)  # all taker buys → maximally toxic
    assert v.value == 1.0
    v2 = VpinTracker(bucket_volume=100, n_buckets=5)
    for _ in range(5):
        v2.add(50, True)
        v2.add(50, False)
    assert v2.value == 0.0
    v3 = VpinTracker(bucket_volume=100, n_buckets=5)
    v3.add(250, True)  # spans buckets: 100 | 100 | 50 pending
    assert len(v3.buckets) == 2 and v3.value == 1.0


def test_kyle_lambda_from_synthetic_impact() -> None:
    """Δmid over each 5 s bucket = λ · signed volume (+ small noise) → estimate ≈ λ."""
    rng = random.Random(7)
    mb = MicroBuilder("BTCUSD", daily_volume=1_000_000)
    lam_true = 0.002  # price units per contract
    mid = 1000.0
    t0 = 5_000_000 * M
    mb.on_quote(t0, mid - 0.5, 10, mid + 0.5, 10)
    for b in range(1, 13):  # 12 buckets in the minute
        q = rng.choice([-400, -200, 200, 400])
        ts = t0 + b * 5 * S - 2 * S
        mb.on_trade(ts, mid, abs(q), q > 0)
        mid += lam_true * q + rng.uniform(-0.05, 0.05)
        mb.on_quote(t0 + b * 5 * S - 1, mid - 0.5, 10, mid + 0.5, 10)
    mb.on_quote(t0 + M + 1, mid - 0.5, 10, mid + 0.5, 10)  # rolls the last bucket
    bar = mb.close(5_000_000 * 60)
    assert bar is not None and bar.kyle_n == 12
    assert bar.kyle_lambda is not None and abs(bar.kyle_lambda - lam_true) < 0.0004
    assert bar.kyle_r2 is not None and bar.kyle_r2 > 0.9
    assert (
        bar.kyle_lambda_bps_per_1k is not None
        and abs(bar.kyle_lambda_bps_per_1k - bar.kyle_lambda * 1000 / mid * 1e4) < 1e-6
    )
    assert bar.realized_vol_bps is not None and bar.realized_vol_bps > 0
    assert bar.trade_intensity == 12 / 60 and bar.max_run >= 1


def test_ofi_l5_and_imbalance_and_tape_stats() -> None:
    mb = MicroBuilder("ETHUSD")
    t0 = 6_000_000 * M
    bids = [Level(100 - i * 0.1, 10) for i in range(5)]
    asks = [Level(100.1 + i * 0.1, 10) for i in range(5)]
    mb.on_l2(t0, bids, asks)
    bids2 = [Level(100 - i * 0.1, 15) for i in range(5)]  # +5 at every bid level
    mb.on_l2(t0 + S, bids2, asks)  # asks unchanged → their contribution nets to 0
    mb.on_quote(t0 + 2 * S, 100.0, 15, 100.1, 10)
    for i in range(25):
        mb.on_trade(t0 + 3 * S + i * 1000, 100.05, 1.0, True)
    mb.on_trade(t0 + 4 * S, 100.05, 50.0, False)  # large trade (≥ 5× median 1.0)
    bar = mb.close(6_000_000 * 60)
    assert bar is not None
    assert bar.ofi_l5 == 25.0  # 5 levels × +5
    assert bar.depth_l5 is not None and abs(bar.depth_l5 - ((50 + 50) + (75 + 50)) / 2) < 1e-9
    assert bar.imbalance is not None and abs(bar.imbalance - (75 - 50) / 125) < 1e-9
    assert bar.large_trade_share is not None and abs(bar.large_trade_share - 50 / 75) < 1e-9
    assert bar.max_run == 25 and bar.avg_trade_size == 75 / 26
    assert mb.close(6_000_000 * 60) is None


def test_quiet_minute_returns_none_and_nan_free() -> None:
    mb = MicroBuilder("SOLUSD", daily_volume=100_000)
    assert mb.close(7_000_000 * 60) is None
    mb.on_quote(7_000_001 * M, 10.0, 1, 10.1, 1)
    bar = mb.close(7_000_001 * 60)
    assert bar is not None and bar.updates == 1 and bar.kyle_lambda is None and bar.vpin is None
    for v in (bar.spread_bps, bar.realized_vol_bps or 0.0):
        assert v is None or not math.isnan(v)
