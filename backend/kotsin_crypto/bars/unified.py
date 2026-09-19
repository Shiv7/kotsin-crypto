"""UnifiedBar — the one record strategies see — plus the store that holds per-symbol, per-timeframe
history and resamples 1m bars into UTC-aligned 5m/15m/30m/1h bars.

Merging is explicit: every optional input carries a ``has_*`` flag; nothing is silently zero.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from .book_bar import BookBar
from .trade_bar import TradeBar

TF_SECONDS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}


@dataclass(frozen=True, slots=True)
class UnifiedBar:
    symbol: str
    tf: str
    ts: int  # bar START, unix seconds, UTC-aligned

    open: float
    high: float
    low: float
    close: float
    volume: float  # contracts
    has_trades: bool = False
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    trade_count: int = 0
    vwap: float | None = None

    has_book: bool = False
    ofi: float | None = None
    microprice: float | None = None
    spread_bps: float | None = None
    depth_imbalance: float | None = None
    book_updates: int = 0

    has_oi: bool = False
    oi: float | None = None

    has_funding: bool = False
    funding_rate: float | None = None

    has_mark: bool = False
    mark_close: float | None = None

    source: str = "live"  # live | rest (backfill)

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3

    @property
    def end_ts(self) -> int:
        return self.ts + TF_SECONDS[self.tf]


def merge_1m(
    trade: TradeBar,
    book: BookBar | None,
    *,
    oi: float | None = None,
    funding_rate: float | None = None,
    mark_close: float | None = None,
    source: str = "live",
) -> UnifiedBar:
    return UnifiedBar(
        symbol=trade.symbol,
        tf="1m",
        ts=trade.ts,
        open=trade.open,
        high=trade.high,
        low=trade.low,
        close=trade.close,
        volume=trade.volume,
        has_trades=trade.trade_count > 0,
        buy_volume=trade.buy_volume,
        sell_volume=trade.sell_volume,
        trade_count=trade.trade_count,
        vwap=trade.vwap,
        has_book=book is not None,
        ofi=book.ofi if book else None,
        microprice=book.microprice if book else None,
        spread_bps=book.spread_bps if book else None,
        depth_imbalance=book.imbalance if book else None,
        book_updates=book.updates if book else 0,
        has_oi=oi is not None,
        oi=oi,
        has_funding=funding_rate is not None,
        funding_rate=funding_rate,
        has_mark=mark_close is not None,
        mark_close=mark_close,
        source=source,
    )


class _Partial:
    __slots__ = ("bars", "bucket", "tf")

    def __init__(self, tf: str, bucket: int) -> None:
        self.tf = tf
        self.bucket = bucket
        self.bars: list[UnifiedBar] = []

    def build(self) -> UnifiedBar:
        return aggregate(self.bars, self.tf, self.bucket)


def aggregate(bars: Sequence[UnifiedBar], tf: str, ts: int) -> UnifiedBar:
    first, last = bars[0], bars[-1]
    vol = sum(b.volume for b in bars)
    pv = sum((b.vwap if b.vwap is not None else b.close) * b.volume for b in bars)
    book_bars = [b for b in bars if b.has_book]
    upd = sum(b.book_updates for b in book_bars)
    spread = sum((b.spread_bps or 0.0) * b.book_updates for b in book_bars) / upd if upd else None
    imb_bars = [b for b in book_bars if b.depth_imbalance is not None]
    imb = sum(b.depth_imbalance or 0.0 for b in imb_bars) / len(imb_bars) if imb_bars else None
    last_book = book_bars[-1] if book_bars else None
    last_oi = next((b.oi for b in reversed(bars) if b.has_oi), None)
    last_fr = next((b.funding_rate for b in reversed(bars) if b.has_funding), None)
    last_mark = next((b.mark_close for b in reversed(bars) if b.has_mark), None)
    return UnifiedBar(
        symbol=first.symbol,
        tf=tf,
        ts=ts,
        open=first.open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=last.close,
        volume=vol,
        has_trades=any(b.has_trades for b in bars),
        buy_volume=sum(b.buy_volume for b in bars),
        sell_volume=sum(b.sell_volume for b in bars),
        trade_count=sum(b.trade_count for b in bars),
        vwap=pv / vol if vol else None,
        has_book=bool(book_bars),
        ofi=sum(b.ofi or 0.0 for b in book_bars) if book_bars else None,
        microprice=last_book.microprice if last_book else None,
        spread_bps=spread,
        depth_imbalance=imb,
        book_updates=upd,
        has_oi=last_oi is not None,
        oi=last_oi,
        has_funding=last_fr is not None,
        funding_rate=last_fr,
        has_mark=last_mark is not None,
        mark_close=last_mark,
        source="rest" if all(b.source == "rest" for b in bars) else "live",
    )


class BarStore:
    def __init__(
        self,
        symbols: Iterable[str],
        tfs: Sequence[str] = ("1m", "5m", "15m", "30m", "1h"),
        maxlen: int = 3000,
    ) -> None:
        assert tfs[0] == "1m"
        self.tfs = tuple(tfs)
        self._bars: dict[str, dict[str, deque[UnifiedBar]]] = {
            s: {tf: deque(maxlen=maxlen) for tf in tfs} for s in symbols
        }
        self._partial: dict[str, dict[str, _Partial | None]] = {
            s: dict.fromkeys(tfs[1:]) for s in symbols
        }
        self.duplicates = 0

    def symbols(self) -> list[str]:
        return list(self._bars)

    def add_1m(self, bar: UnifiedBar) -> list[UnifiedBar]:
        """Append a closed 1m bar; return the higher-timeframe bars it closed (in tf order)."""
        d = self._bars[bar.symbol]["1m"]
        if d and bar.ts <= d[-1].ts:
            self.duplicates += 1
            return []
        d.append(bar)
        closed: list[UnifiedBar] = []
        for tf in self.tfs[1:]:
            closed.extend(self._resample(bar, tf))
        return closed

    def _resample(self, bar: UnifiedBar, tf: str) -> list[UnifiedBar]:
        s = TF_SECONDS[tf]
        bucket = bar.ts - bar.ts % s
        out: list[UnifiedBar] = []
        partial = self._partial[bar.symbol][tf]
        if partial is not None and partial.bucket != bucket:
            out.append(self._close(partial))
            partial = None
        if partial is None:
            partial = _Partial(tf, bucket)
            self._partial[bar.symbol][tf] = partial
        partial.bars.append(replace(bar))
        if bar.ts + 60 >= bucket + s:  # last minute of the bucket → close immediately
            out.append(self._close(partial))
        return out

    def _close(self, partial: _Partial) -> UnifiedBar:
        closed = partial.build()
        self._bars[closed.symbol][closed.tf].append(closed)
        self._partial[closed.symbol][closed.tf] = None
        return closed

    def forming(self, symbol: str, tf: str, forming_1m: UnifiedBar | None) -> UnifiedBar | None:
        """The still-open bar for ``tf``: the closed 1m bars of the current bucket plus the forming minute."""
        if tf == "1m":
            return forming_1m
        s = TF_SECONDS[tf]
        partial = self._partial[symbol][tf]
        bars: list[UnifiedBar] = list(partial.bars) if partial else []
        bucket = partial.bucket if partial else None
        if forming_1m is not None:
            b1 = forming_1m.ts - forming_1m.ts % s
            if bucket is None or b1 == bucket:
                bars.append(forming_1m)
                bucket = b1
            elif b1 > bucket:
                bars, bucket = [forming_1m], b1
        if not bars or bucket is None:
            return None
        return replace(aggregate(bars, tf, bucket), source="forming")

    def bars(self, symbol: str, tf: str, n: int) -> list[UnifiedBar]:
        d = self._bars[symbol][tf]
        return list(d)[-n:] if n < len(d) else list(d)

    def last(self, symbol: str, tf: str) -> UnifiedBar | None:
        d = self._bars[symbol][tf]
        return d[-1] if d else None

    def counts(self) -> dict[str, dict[str, int]]:
        return {s: {tf: len(d) for tf, d in tfs.items()} for s, tfs in self._bars.items()}
