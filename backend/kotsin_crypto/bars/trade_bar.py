"""1-minute bars from the trade tape. Pure and incremental: the same trades in the same order always
produce the same bars (the determinism test relies on this).

Minutes with no trades still produce a bar (OHLC = last close, volume 0, ``trade_count`` 0) so the
clock keeps moving in quiet periods; the unified bar marks them ``has_trades=False``.
"""

from __future__ import annotations

from dataclasses import dataclass

MINUTE_US = 60_000_000


@dataclass(slots=True)
class TradeBar:
    symbol: str
    ts: int  # minute start, unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_volume: float
    sell_volume: float
    trade_count: int
    vwap: float | None
    first_ts_us: int | None
    last_ts_us: int | None


class TradeBarBuilder:
    GRACE_S = 1.0  # a minute is closed this long after its end, to let straggling trades land

    def __init__(
        self, symbol: str, *, last_close: float | None = None, last_minute: int | None = None
    ) -> None:
        self.symbol = symbol
        self._cur: TradeBar | None = None
        self._pv = 0.0
        self._last_close = last_close
        self._last_emitted = (
            last_minute  # minute index (unix seconds // 60) of the last emitted bar
        )
        self.late_trades = 0

    @property
    def last_close(self) -> float | None:
        return self._last_close

    def on_trade(self, ts_us: int, price: float, size: float, taker_buy: bool) -> list[TradeBar]:
        minute = ts_us // MINUTE_US
        out: list[TradeBar] = []
        if self._last_emitted is not None and minute <= self._last_emitted:
            self.late_trades += 1
            return out
        if self._cur is not None and minute > self._cur.ts // 60:
            out.extend(self._close_through(minute - 1))
        if self._cur is None:
            if self._last_emitted is not None and minute > self._last_emitted + 1:
                out.extend(self._empty_bars(self._last_emitted + 1, minute - 1))
            self._cur = TradeBar(
                self.symbol,
                minute * 60,
                price,
                price,
                price,
                price,
                0.0,
                0.0,
                0.0,
                0,
                None,
                ts_us,
                ts_us,
            )
            self._pv = 0.0
        b = self._cur
        b.high = max(b.high, price)
        b.low = min(b.low, price)
        b.close = price
        b.volume += size
        if taker_buy:
            b.buy_volume += size
        else:
            b.sell_volume += size
        b.trade_count += 1
        self._pv += price * size
        b.vwap = self._pv / b.volume if b.volume else None
        b.last_ts_us = ts_us
        return out

    def flush(self, now_s: float) -> list[TradeBar]:
        """Close every minute that ended more than GRACE_S ago."""
        complete_upto = int((now_s - self.GRACE_S) // 60) - 1
        if self._cur is not None:
            if self._cur.ts // 60 <= complete_upto:
                return self._close_through(complete_upto)
            return []
        if self._last_emitted is not None and complete_upto > self._last_emitted:
            return self._empty_bars(self._last_emitted + 1, complete_upto)
        return []

    def _close_through(self, upto_minute: int) -> list[TradeBar]:
        assert self._cur is not None
        cur = self._cur
        cur_minute = cur.ts // 60
        self._cur = None
        self._last_close = cur.close
        self._last_emitted = cur_minute
        out = [cur]
        if upto_minute > cur_minute:
            out.extend(self._empty_bars(cur_minute + 1, upto_minute))
        return out

    def _empty_bars(self, m0: int, m1: int) -> list[TradeBar]:
        lc = self._last_close
        if lc is None:
            return []
        out = [
            TradeBar(self.symbol, m * 60, lc, lc, lc, lc, 0.0, 0.0, 0.0, 0, None, None, None)
            for m in range(m0, m1 + 1)
        ]
        self._last_emitted = m1
        return out
