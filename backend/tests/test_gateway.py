from __future__ import annotations

from kotsin_crypto.domain import OrderIntent, OrderSide, Purpose
from kotsin_crypto.exec.gateway import Decision, Gateway, Mode
from kotsin_crypto.exec.paper import PaperMatcher
from kotsin_crypto.feed.book import Book, Level


def _book() -> Book:
    b = Book("BTCUSD")
    b.replace(
        [Level(100.0, 50), Level(99.5, 100)],
        [Level(100.5, 50), Level(101.0, 100)],
        ts_us=1_000_000_000,
    )
    return b


def _intent(coid: str = "sig-1", purpose: Purpose = Purpose.ENTRY) -> OrderIntent:
    return OrderIntent(
        strategy="CAN2",
        symbol="BTCUSD",
        side=OrderSide.BUY,
        contracts=60,
        purpose=purpose,
        signal_id="sig-1",
        client_order_id=coid,
    )


def _gw(mode: Mode, halted: bool = False) -> Gateway:
    return Gateway(
        books={"BTCUSD": _book()},
        matcher=PaperMatcher(),
        mode=lambda: mode,
        halted=lambda: (halted, "test halt"),
    )


def test_paper_fill_walks_book_and_charges_fee() -> None:
    gw = _gw(Mode.PAPER)
    r = gw.submit(_intent(), contract_value=0.001, now_us=1_000_500_000)
    assert r.decision is Decision.PAPER_FILLED and r.fill is not None
    assert r.fill.contracts == 60 and abs(r.fill.price - (50 * 100.5 + 10 * 101.0) / 60) < 1e-9
    assert r.fill.slippage_bps > 0 and abs(r.fill.fee - r.fill.price * 60 * 0.001 * 0.0005) < 1e-12
    assert r.order.status == "FILLED" and gw.orders_today == 1


def test_duplicate_is_blocked_and_shadow_places_nothing() -> None:
    gw = _gw(Mode.PAPER)
    gw.submit(_intent(), contract_value=0.001, now_us=1_000_500_000)
    dup = gw.submit(_intent(), contract_value=0.001, now_us=1_000_500_000)
    assert dup.decision is Decision.DUP_BLOCKED and dup.fill is None
    sh = _gw(Mode.SHADOW).submit(_intent(), contract_value=0.001)
    assert sh.decision is Decision.SHADOW_OK and sh.order.status == "SHADOW" and sh.fill is None


def test_halt_blocks_entries_but_not_exits() -> None:
    gw = _gw(Mode.PAPER, halted=True)
    assert (
        gw.submit(_intent("a"), contract_value=0.001, now_us=1_000_500_000).decision
        is Decision.REJECTED_HALT
    )
    assert (
        gw.submit(_intent("b", Purpose.EXIT), contract_value=0.001, now_us=1_000_500_000).decision
        is Decision.PAPER_FILLED
    )


def test_stale_book_is_refused_and_live_is_refused_until_step_8() -> None:
    gw = _gw(Mode.PAPER)
    r = gw.submit(_intent(), contract_value=0.001, now_us=1_000_000_000 + 10_000_000)
    assert r.decision is Decision.REJECTED_VENUE and "ms old" in r.order.note
    live = _gw(Mode.LIVE).submit(_intent(), contract_value=0.001)
    assert live.decision is Decision.REJECTED_VENUE and "step 8" in live.order.note
