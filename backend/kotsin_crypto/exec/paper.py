"""Paper matching against the live L2 book. A market order walks the ladder; the fill records the
weighted price, slippage vs mid, the book age at decision time and the taker fee."""

from __future__ import annotations

import time

from ..domain import Fill, OrderIntent
from ..feed.book import Book


class NoBook(Exception):
    pass


class PaperMatcher:
    def __init__(self, *, taker_fee_rate: float = 0.0005, max_book_age_ms: int = 5000) -> None:
        self.taker_fee_rate = taker_fee_rate
        self.max_book_age_ms = max_book_age_ms

    def fill(
        self,
        intent: OrderIntent,
        book: Book | None,
        *,
        contract_value: float,
        now_us: int | None = None,
    ) -> Fill:
        now_us = now_us or int(time.time() * 1e6)
        if book is None or book.mid is None:
            raise NoBook(f"no book for {intent.symbol}")
        age = book.age_ms(now_us)
        if age is not None and age > self.max_book_age_ms:
            raise NoBook(f"book for {intent.symbol} is {age} ms old")
        walk = book.walk(intent.side.value, intent.contracts)
        if walk.filled == 0:
            raise NoBook(f"empty {intent.side.value} side for {intent.symbol}")
        mid = book.mid
        slip = (walk.avg_price - mid) / mid * 1e4 * (1 if intent.side.value == "BUY" else -1)
        fee = walk.avg_price * walk.filled * contract_value * self.taker_fee_rate
        return Fill(
            price=walk.avg_price,
            contracts=walk.filled,
            ts=now_us / 1e6,
            fee=fee,
            slippage_bps=slip,
            book_age_ms=age,
            levels=walk.levels,
        )
