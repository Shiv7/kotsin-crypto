"""Trading-R1 §3.5 volatility-driven discretisation, adapted to intraday crypto.

Forward returns over several horizons are each normalised by rolling realised volatility (Sharpe-like
signals), combined with weights, and mapped to five classes. Trading-R1 uses 3/7/15-day horizons with
weights 0.3/0.5/0.2 and asymmetric percentile cut-offs (85/53/15/3) that keep equities' upward drift;
here the horizons default to 1h/4h/24h on 5m bars and the cut-offs are symmetric because a perpetual
has no structural drift. ``z_label`` scores a single observation (used to grade committee decisions);
``label_series`` labels history (for supervised work and calibration)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from .schemas import RATING_RANK, Rating

RATINGS_ORDERED = [Rating.STRONG_SELL, Rating.SELL, Rating.HOLD, Rating.BUY, Rating.STRONG_BUY]


def z_label(z: float, strong: float = 1.0, weak: float = 0.25) -> Rating:
    if z >= strong:
        return Rating.STRONG_BUY
    if z >= weak:
        return Rating.BUY
    if z <= -strong:
        return Rating.STRONG_SELL
    if z <= -weak:
        return Rating.SELL
    return Rating.HOLD


def ordinal_score(predicted: Rating, truth: Rating) -> float:
    """1 for an exact hit, partial credit for adjacent classes, 0 for the opposite extreme
    (the shape of Trading-R1's decision reward)."""
    return 1.0 - abs(RATING_RANK[predicted] - RATING_RANK[truth]) / 4.0


def vol_adjusted_z(
    closes: Sequence[float],
    horizons: Sequence[int] = (12, 48, 288),
    weights: Sequence[float] = (0.3, 0.5, 0.2),
    vol_window: int = 20,
) -> np.ndarray:
    """Composite forward signal per index (NaN where a horizon runs past the end)."""
    c = np.asarray(closes, dtype=float)
    n = len(c)
    logret = np.diff(np.log(c), prepend=np.nan)
    out = np.full(n, np.nan)
    if n < vol_window + 2:
        return out
    # rolling per-bar vol
    vol = np.full(n, np.nan)
    for i in range(vol_window, n):
        w = logret[i - vol_window + 1 : i + 1]
        vol[i] = np.nanstd(w) if np.isfinite(w).sum() >= 3 else np.nan
    comp = np.zeros(n)
    wsum = np.zeros(n)
    for h, w in zip(horizons, weights, strict=True):
        if h >= n:  # horizon longer than the series: this component contributes nothing
            continue
        fwd = np.full(n, np.nan)
        fwd[: n - h] = np.log(c[h:] / c[: n - h])
        with np.errstate(divide="ignore", invalid="ignore"):
            z = fwd / (vol * math.sqrt(h))
        ok = np.isfinite(z)
        comp[ok] += w * z[ok]
        wsum[ok] += w
    good = wsum > 0
    out[good] = comp[good] / wsum[good]
    return out


def label_series(
    closes: Sequence[float], quantiles: Sequence[float] = (0.85, 0.53, 0.15, 0.03), **kw: object
) -> tuple[np.ndarray, list[Rating | None]]:
    """Ratings for each index by percentile cut-offs of the composite signal (Trading-R1's scheme:
    top 15 % STRONG_BUY, next 32 % BUY, 38 % HOLD, 12 % SELL, bottom 3 % STRONG_SELL by default)."""
    z = vol_adjusted_z(closes, **kw)  # type: ignore[arg-type]
    valid = z[np.isfinite(z)]
    labels: list[Rating | None] = [None] * len(z)
    if len(valid) < 20:
        return z, labels
    q_sb, q_b, q_s, q_ss = (np.quantile(valid, q) for q in quantiles)
    for i, v in enumerate(z):
        if not np.isfinite(v):
            continue
        if v >= q_sb:
            labels[i] = Rating.STRONG_BUY
        elif v >= q_b:
            labels[i] = Rating.BUY
        elif v <= q_ss:
            labels[i] = Rating.STRONG_SELL
        elif v <= q_s:
            labels[i] = Rating.SELL
        else:
            labels[i] = Rating.HOLD
    return z, labels
