"""Per-minute microstructure metrics from the trade tape (taker side known), L1 quotes (10/s) and
L2 snapshots (2/s). One builder per symbol; ``close(minute_ts)`` returns a ``MicroBar``.

Kyle's lambda    price impact: OLS through the origin of Δmid on signed volume over 5 s sub-buckets,
                 reported for the minute (12 buckets, noisy) and rolling 15 min (180 buckets). Also
                 normalised to bps of price move per 1,000 contracts.
VPIN             Easley–López de Prado–O'Hara toxicity: |buy − sell| per volume bucket averaged over
                 the last 50 buckets, using the venue's real taker side (not bulk-volume classification).
                 ``vpin`` uses buckets of 1/50 of daily volume (≈ one-day window); ``vpin_fast`` uses
                 1/500 (≈ 2–3 h window).
OFI (L1)         Cont–Kukanov–Stoikov best-quote order-flow imbalance from L1 quote changes.
OFI (L5)         multi-level OFI (Xu, Gould et al.): the same flow accounting applied rank-by-rank to
                 the top 5 levels of consecutive L2 snapshots, plus a depth-normalised version.
Realised vol     sqrt(Σ r²) of 5 s mid log-returns inside the minute, in bps.
Tape             trade intensity (trades/s), large-trade share (size ≥ 5× rolling median), longest
                 same-side run, average trade size.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from statistics import median

from ..domain import Level

MINUTE_US = 60_000_000


@dataclass(slots=True)
class MicroBar:
    symbol: str
    ts: int
    updates: int
    ofi: float
    microprice: float | None
    spread_bps: float | None
    imbalance: float | None
    ofi_l5: float | None
    ofi_l5_norm: float | None
    depth_l5: float | None
    kyle_lambda: float | None
    kyle_lambda_bps_per_1k: float | None
    kyle_r2: float | None
    kyle_n: int
    kyle_lambda_15m: float | None
    kyle_lambda_15m_bps_per_1k: float | None
    kyle_15m_r2: float | None
    vpin: float | None
    vpin_fast: float | None
    vpin_bucket_volume: float | None
    realized_vol_bps: float | None
    trade_intensity: float
    large_trade_share: float | None
    max_run: int
    avg_trade_size: float | None


def ols_origin(pairs: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    """Slope and R² of y = λx through the origin."""
    if len(pairs) < 3:
        return None, None
    sxy = sum(y * x for y, x in pairs)
    sxx = sum(x * x for _, x in pairs)
    syy = sum(y * y for y, _ in pairs)
    if sxx <= 0:
        return None, None
    lam = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else None
    return lam, r2


class VpinTracker:
    def __init__(self, bucket_volume: float, n_buckets: int = 50) -> None:
        self.bucket_volume = bucket_volume
        self.buckets: deque[float] = deque(maxlen=n_buckets)
        self._buy = 0.0
        self._sell = 0.0
        self._filled = 0.0

    def add(self, size: float, taker_buy: bool) -> None:
        while size > 0:
            take = min(self.bucket_volume - self._filled, size)
            if taker_buy:
                self._buy += take
            else:
                self._sell += take
            self._filled += take
            size -= take
            if self._filled >= self.bucket_volume - 1e-9:
                self.buckets.append(abs(self._buy - self._sell))
                self._buy = self._sell = self._filled = 0.0

    @property
    def value(self) -> float | None:
        if not self.buckets:
            return None
        return sum(self.buckets) / (len(self.buckets) * self.bucket_volume)


@dataclass(slots=True)
class _Acc:
    n_quotes: int = 0
    ofi: float = 0.0
    spread_sum: float = 0.0
    imb_sum: float = 0.0
    imb_n: int = 0
    micro: float | None = None
    n_l2: int = 0
    ofi_l5: float = 0.0
    depth_l5_sum: float = 0.0
    trades: int = 0
    volume: float = 0.0
    large_volume: float = 0.0
    run: int = 0
    max_run: int = 0
    last_side: bool | None = None
    rv_sum: float = 0.0
    kyle_pairs: list[tuple[float, float]] = field(default_factory=list)


class MicroBuilder:
    def __init__(
        self,
        symbol: str,
        *,
        daily_volume: float | None = None,
        bucket_s: int = 5,
        kyle_window_buckets: int = 180,
        vpin_buckets: int = 50,
    ) -> None:
        self.symbol = symbol
        self.bucket_us = bucket_s * 1_000_000
        self._acc: dict[int, _Acc] = {}
        self._prev_quote: tuple[float, int, float, int] | None = None
        self._prev_l2: tuple[list[Level], list[Level]] | None = None
        self._depth5: tuple[int, int] | None = None
        self._kyle_roll: deque[tuple[float, float]] = deque(maxlen=kyle_window_buckets)
        self._sizes: deque[float] = deque(maxlen=500)
        self.daily_volume = daily_volume
        self._vpin = VpinTracker(daily_volume / 50, vpin_buckets) if daily_volume else None
        self._vpin_fast = VpinTracker(daily_volume / 500, vpin_buckets) if daily_volume else None
        self._mid: float | None = None
        self._bucket_id: int | None = None
        self._bucket_q = 0.0
        self._bucket_start_mid: float | None = None

    # ---- inputs ----------------------------------------------------------------------------------
    def on_quote(self, ts_us: int, bid: float, bid_size: int, ask: float, ask_size: int) -> None:
        self._roll_bucket(ts_us)
        acc = self._acc.setdefault(ts_us // MINUTE_US, _Acc())
        if self._prev_quote is not None:
            pb, pbs, pa, pas = self._prev_quote
            e = 0.0
            if bid >= pb:
                e += bid_size
            if bid <= pb:
                e -= pbs
            if ask <= pa:
                e -= ask_size
            if ask >= pa:
                e += pas
            acc.ofi += e
        mid = (bid + ask) / 2
        self._mid = mid
        if self._bucket_start_mid is None:  # first quote ever: anchor the open bucket
            self._bucket_start_mid = mid
        if mid > 0:
            acc.spread_sum += (ask - bid) / mid * 1e4
        if self._depth5 is not None:
            db, da = self._depth5
            if db + da:
                acc.imb_sum += (db - da) / (db + da)
                acc.imb_n += 1
        qs = bid_size + ask_size
        acc.micro = (bid * ask_size + ask * bid_size) / qs if qs else mid
        acc.n_quotes += 1
        self._prev_quote = (bid, bid_size, ask, ask_size)

    def on_l2(self, ts_us: int, bids: list[Level], asks: list[Level]) -> None:
        acc = self._acc.setdefault(ts_us // MINUTE_US, _Acc())
        b5, a5 = bids[:5], asks[:5]
        self._depth5 = (sum(lv.size for lv in b5), sum(lv.size for lv in a5))
        if self._prev_l2 is not None:
            pb5, pa5 = self._prev_l2
            e = 0.0
            for m in range(min(len(b5), len(pb5))):
                if b5[m].price >= pb5[m].price:
                    e += b5[m].size
                if b5[m].price <= pb5[m].price:
                    e -= pb5[m].size
            for m in range(min(len(a5), len(pa5))):
                if a5[m].price <= pa5[m].price:
                    e -= a5[m].size
                if a5[m].price >= pa5[m].price:
                    e += pa5[m].size
            acc.ofi_l5 += e
        acc.depth_l5_sum += self._depth5[0] + self._depth5[1]
        acc.n_l2 += 1
        self._prev_l2 = (b5, a5)

    def on_trade(self, ts_us: int, price: float, size: float, taker_buy: bool) -> None:
        self._roll_bucket(ts_us)
        acc = self._acc.setdefault(ts_us // MINUTE_US, _Acc())
        acc.trades += 1
        acc.volume += size
        if len(self._sizes) >= 20 and size >= 5 * median(self._sizes):
            acc.large_volume += size
        self._sizes.append(size)
        if acc.last_side is taker_buy:
            acc.run += 1
        else:
            acc.run = 1
            acc.last_side = taker_buy
        acc.max_run = max(acc.max_run, acc.run)
        self._bucket_q += size if taker_buy else -size
        if self._mid is None:
            self._mid = price
        if self._bucket_start_mid is None:
            self._bucket_start_mid = self._mid
        if self._vpin is not None:
            self._vpin.add(size, taker_buy)
            assert self._vpin_fast is not None
            self._vpin_fast.add(size, taker_buy)

    def _roll_bucket(self, ts_us: int) -> None:
        bid = ts_us // self.bucket_us
        if self._bucket_id is None:
            self._bucket_id = bid
            self._bucket_start_mid = self._mid
            return
        if bid == self._bucket_id:
            return
        # close the finished 5 s bucket: Δmid over the bucket vs the signed volume inside it
        if (
            self._mid is not None
            and self._bucket_start_mid is not None
            and self._bucket_start_mid > 0
        ):
            dmid = self._mid - self._bucket_start_mid
            pair = (dmid, self._bucket_q)
            self._kyle_roll.append(pair)
            acc = self._acc.setdefault((self._bucket_id * self.bucket_us) // MINUTE_US, _Acc())
            acc.kyle_pairs.append(pair)
            r = math.log(self._mid / self._bucket_start_mid)
            acc.rv_sum += r * r
        self._bucket_id = bid
        self._bucket_q = 0.0
        self._bucket_start_mid = self._mid

    # ---- output ----------------------------------------------------------------------------------
    def close(self, minute_ts: int) -> MicroBar | None:
        minute = minute_ts // 60
        for m in [m for m in self._acc if m < minute]:
            del self._acc[m]
        acc = self._acc.pop(minute, None)
        if acc is None or (acc.n_quotes == 0 and acc.trades == 0 and acc.n_l2 == 0):
            return None
        mid = self._mid
        lam, r2 = ols_origin(acc.kyle_pairs)
        lam15, r2_15 = ols_origin(list(self._kyle_roll))

        def bps_per_1k(lmb: float | None) -> float | None:
            return lmb * 1000 / mid * 1e4 if (lmb is not None and mid) else None

        return MicroBar(
            symbol=self.symbol,
            ts=minute_ts,
            updates=acc.n_quotes,
            ofi=acc.ofi,
            microprice=acc.micro,
            spread_bps=acc.spread_sum / acc.n_quotes if acc.n_quotes else None,
            imbalance=acc.imb_sum / acc.imb_n if acc.imb_n else None,
            ofi_l5=acc.ofi_l5 if acc.n_l2 > 1 else None,
            ofi_l5_norm=(acc.ofi_l5 / (acc.depth_l5_sum / acc.n_l2))
            if acc.n_l2 > 1 and acc.depth_l5_sum > 0
            else None,
            depth_l5=acc.depth_l5_sum / acc.n_l2 if acc.n_l2 else None,
            kyle_lambda=lam,
            kyle_lambda_bps_per_1k=bps_per_1k(lam),
            kyle_r2=r2,
            kyle_n=len(acc.kyle_pairs),
            kyle_lambda_15m=lam15,
            kyle_lambda_15m_bps_per_1k=bps_per_1k(lam15),
            kyle_15m_r2=r2_15,
            vpin=self._vpin.value if self._vpin else None,
            vpin_fast=self._vpin_fast.value if self._vpin_fast else None,
            vpin_bucket_volume=self._vpin.bucket_volume if self._vpin else None,
            realized_vol_bps=math.sqrt(acc.rv_sum) * 1e4
            if acc.rv_sum > 0
            else (0.0 if acc.kyle_pairs else None),
            trade_intensity=acc.trades / 60.0,
            large_trade_share=acc.large_volume / acc.volume
            if acc.volume > 0 and len(self._sizes) >= 20
            else None,
            max_run=acc.max_run,
            avg_trade_size=acc.volume / acc.trades if acc.trades else None,
        )
