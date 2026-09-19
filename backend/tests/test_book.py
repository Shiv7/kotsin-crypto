from __future__ import annotations

from kotsin_crypto.feed.book import Book, Level


def _book() -> Book:
    b = Book("BTCUSD")
    b.replace(
        bids=[Level(100.0, 5), Level(99.5, 10), Level(99.0, 20)],
        asks=[Level(100.5, 4), Level(101.0, 10), Level(101.5, 30)],
        ts_us=1_000_000,
    )
    return b


def test_quotes_and_microprice() -> None:
    b = _book()
    assert b.best_bid and b.best_bid.price == 100.0 and b.best_ask and b.best_ask.price == 100.5
    assert b.mid == 100.25
    assert abs(b.spread_bps - 0.5 / 100.25 * 1e4) < 1e-9
    assert abs(b.microprice - (100.0 * 4 + 100.5 * 5) / 9) < 1e-9
    assert b.depth("bid", 2) == 15 and b.imbalance(3) == (35 - 44) / 79


def test_walk_consumes_the_ladder() -> None:
    b = _book()
    w = b.walk("BUY", 10)
    assert w.complete and w.levels == 2 and w.worst_price == 101.0
    assert abs(w.avg_price - (4 * 100.5 + 6 * 101.0) / 10) < 1e-9
    w2 = b.walk("SELL", 100)
    assert not w2.complete and w2.filled == 35 and w2.worst_price == 99.0


def test_age() -> None:
    b = _book()
    assert b.age_ms(3_000_000) == 2000
    assert Book("X").age_ms(5) is None
