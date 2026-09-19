"""1-minute book metrics from successive L2 snapshots: order-flow imbalance (Cont–Kukanov–Stoikov
best-quote OFI), mean spread, mean top-5 depth imbalance, last microprice, update count."""

from __future__ import annotations

from dataclasses import dataclass

MINUTE_US = 60_000_000


@dataclass(slots=True)
class BookBar:
    symbol: str
    ts: int
    ofi: float
    microprice: float | None
    spread_bps: float | None
    imbalance: float | None
    updates: int


@dataclass(slots=True)
class _Acc:
    ofi: float = 0.0
    spread_sum: float = 0.0
    imb_sum: float = 0.0
    imb_n: int = 0
    micro: float | None = None
    n: int = 0


class BookBarBuilder:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._acc: dict[int, _Acc] = {}
        self._prev: tuple[float, int, float, int] | None = None

    def on_book(
        self,
        ts_us: int,
        best_bid: float,
        bid_size: int,
        best_ask: float,
        ask_size: int,
        depth_bid: int,
        depth_ask: int,
    ) -> None:
        minute = ts_us // MINUTE_US
        acc = self._acc.setdefault(minute, _Acc())
        if self._prev is not None:
            pb, pbs, pa, pas = self._prev
            e = 0.0
            if best_bid >= pb:
                e += bid_size
            if best_bid <= pb:
                e -= pbs
            if best_ask <= pa:
                e -= ask_size
            if best_ask >= pa:
                e += pas
            acc.ofi += e
        mid = (best_bid + best_ask) / 2
        if mid > 0:
            acc.spread_sum += (best_ask - best_bid) / mid * 1e4
        tot = depth_bid + depth_ask
        if tot:
            acc.imb_sum += (depth_bid - depth_ask) / tot
            acc.imb_n += 1
        qs = bid_size + ask_size
        acc.micro = (best_bid * ask_size + best_ask * bid_size) / qs if qs else mid
        acc.n += 1
        self._prev = (best_bid, bid_size, best_ask, ask_size)

    def close(self, minute_ts: int) -> BookBar | None:
        """Return the bar for the minute starting at ``minute_ts`` (unix s) and drop older accumulators."""
        minute = minute_ts // 60
        for m in [m for m in self._acc if m < minute]:
            del self._acc[m]
        acc = self._acc.pop(minute, None)
        if acc is None or acc.n == 0:
            return None
        return BookBar(
            symbol=self.symbol,
            ts=minute_ts,
            ofi=acc.ofi,
            microprice=acc.micro,
            spread_bps=acc.spread_sum / acc.n,
            imbalance=acc.imb_sum / acc.imb_n if acc.imb_n else None,
            updates=acc.n,
        )
