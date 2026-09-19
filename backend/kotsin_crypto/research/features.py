"""Feature frames for research and RL: one row per 5m bar (built from 1m history via the same
BarStore resampler the engine uses), with multi-horizon returns, realised volatility, distance to
rolling VWAP and range, volume surge, and — when the bars carry a tape (archive replay) — the
microstructure fields. Point-in-time: every feature at row i uses bars ≤ i only. Forward returns and
the Trading-R1 labels are appended as separate, clearly-named target columns."""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import median
from typing import Any

import numpy as np

from ..bars.unified import BarStore, UnifiedBar
from ..committee.labels import label_series

FEATURE_COLUMNS = [
    "ret_1",
    "ret_3",
    "ret_12",
    "ret_48",
    "ret_288",
    "rv_12",
    "rv_48",
    "range_pct_12",
    "pos_in_range_48",
    "vwap_dist_12",
    "surge_20",
    "hl_atr_14_pct",
    "hour_sin",
    "hour_cos",
    "dow",
    "buy_ratio",
    "ofi_norm",
    "kyle_bps",
    "vpin_fast",
    "rv_bps_1m",
    "spread_bps",
    "funding_pct",
    "has_micro",
]
TARGET_COLUMNS = ["fwd_ret_12", "fwd_ret_48", "fwd_ret_288", "label_z", "label"]


def resample_5m(symbol: str, bars_1m: Sequence[UnifiedBar]) -> list[UnifiedBar]:
    store = BarStore([symbol], tfs=("1m", "5m"), maxlen=max(10_000, len(bars_1m) + 10))
    for b in bars_1m:
        store.add_1m(b)
    return store.bars(symbol, "5m", len(bars_1m))


def _logret(a: float, b: float) -> float:
    return math.log(a / b) if a > 0 and b > 0 else 0.0


def build_frame(symbol: str, bars5: Sequence[UnifiedBar]) -> dict[str, Any]:
    """Columns → numpy arrays (float), aligned to ``bars5``. NaN where undefined."""
    n = len(bars5)
    closes = np.array([b.close for b in bars5], dtype=float)
    highs = np.array([b.high for b in bars5], dtype=float)
    lows = np.array([b.low for b in bars5], dtype=float)
    vols = np.array([b.volume for b in bars5], dtype=float)
    lr = np.full(n, np.nan)
    lr[1:] = np.log(closes[1:] / closes[:-1])
    cols: dict[str, np.ndarray] = {c: np.full(n, np.nan) for c in FEATURE_COLUMNS + TARGET_COLUMNS}
    cols["ts"] = np.array([b.ts for b in bars5], dtype=np.int64)
    cols["close"] = closes
    for k in (1, 3, 12, 48, 288):
        if n > k:
            cols[f"ret_{k}"][k:] = np.log(closes[k:] / closes[:-k])
    for w in (12, 48):
        for i in range(w, n):
            x = lr[i - w + 1 : i + 1]
            cols[f"rv_{w}"][i] = float(np.nanstd(x)) * math.sqrt(w)
    for i in range(12, n):
        hi, lo = highs[i - 11 : i + 1].max(), lows[i - 11 : i + 1].min()
        cols["range_pct_12"][i] = (hi - lo) / lo if lo else np.nan
        pv = sum(b.typical * b.volume for b in bars5[i - 11 : i + 1])
        vv = vols[i - 11 : i + 1].sum()
        cols["vwap_dist_12"][i] = (closes[i] / (pv / vv) - 1) if vv > 0 else np.nan
    for i in range(48, n):
        hi, lo = highs[i - 47 : i + 1].max(), lows[i - 47 : i + 1].min()
        cols["pos_in_range_48"][i] = (closes[i] - lo) / (hi - lo) if hi > lo else 0.5
    for i in range(20, n):
        med = median(vols[i - 20 : i].tolist())
        cols["surge_20"][i] = vols[i] / med if med > 0 else np.nan
    for i in range(14, n):
        trs = [
            max(
                bars5[j].high - bars5[j].low,
                abs(bars5[j].high - bars5[j - 1].close),
                abs(bars5[j].low - bars5[j - 1].close),
            )
            for j in range(i - 13, i + 1)
        ]
        cols["hl_atr_14_pct"][i] = (sum(trs) / 14) / closes[i]
    for i, b in enumerate(bars5):
        hour = (b.ts % 86400) / 3600
        cols["hour_sin"][i] = math.sin(2 * math.pi * hour / 24)
        cols["hour_cos"][i] = math.cos(2 * math.pi * hour / 24)
        cols["dow"][i] = ((b.ts // 86400) + 4) % 7  # 0 = Monday
        cols["has_micro"][i] = 1.0 if b.has_micro else 0.0
        cols["buy_ratio"][i] = (
            (b.buy_volume / b.volume) if (b.has_micro and b.volume > 0) else np.nan
        )
        cols["ofi_norm"][i] = b.ofi_l5_norm if b.ofi_l5_norm is not None else np.nan
        cols["kyle_bps"][i] = (
            b.kyle_lambda_15m_bps_per_1k if b.kyle_lambda_15m_bps_per_1k is not None else np.nan
        )
        cols["vpin_fast"][i] = b.vpin_fast if b.vpin_fast is not None else np.nan
        cols["rv_bps_1m"][i] = b.realized_vol_bps if b.realized_vol_bps is not None else np.nan
        cols["spread_bps"][i] = b.spread_bps if b.spread_bps is not None else np.nan
        cols["funding_pct"][i] = b.funding_rate if b.funding_rate is not None else np.nan
    for k in (12, 48, 288):
        if n > k:
            cols[f"fwd_ret_{k}"][: n - k] = np.log(closes[k:] / closes[:-k])
    z, labels = label_series(closes)
    cols["label_z"] = z
    cols["label"] = np.array(
        [
            {"STRONG_SELL": -2, "SELL": -1, "HOLD": 0, "BUY": 1, "STRONG_BUY": 2}[lab.value]
            if lab
            else np.nan
            for lab in labels
        ],
        dtype=float,
    )
    return cols


def regime_of(row: dict[str, float]) -> str:
    """Coarse context for the bandit: volatility tercile × session. Cut-offs are fixed so the label
    is stable across runs (recalibrate deliberately, not silently)."""
    rv = row.get("rv_48")
    hour = math.atan2(row.get("hour_sin", 0.0), row.get("hour_cos", 1.0)) / (2 * math.pi) * 24 % 24
    vol = (
        "vol_low"
        if rv is None or math.isnan(rv) or rv < 0.004
        else ("vol_mid" if rv < 0.009 else "vol_high")
    )
    session = "asia" if hour < 7 else ("eu" if hour < 13 else ("us" if hour < 21 else "late"))
    return f"{vol}|{session}"
