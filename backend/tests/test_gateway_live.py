"""LIVE_CAPPED / LIVE through the gateway with a fake executor: the sizing rule, every cap, exits
never capped, and the SUBMITTED decision only on a venue fill."""

from __future__ import annotations

from typing import Any

from kotsin_crypto.domain import Fill, OrderIntent, OrderSide, Position, PosSide, Purpose
from kotsin_crypto.exec.gateway import (
    Decision,
    Gateway,
    LiveCaps,
    LiveContext,
    Mode,
    live_capped_contracts,
)
from kotsin_crypto.exec.live import LiveResult
from kotsin_crypto.exec.paper import PaperMatcher


class FakeLive:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.entries: list[tuple[OrderIntent, float]] = []
        self.exits: list[tuple[Position, OrderIntent]] = []

    async def place_entry(self, intent: OrderIntent, stop: float) -> LiveResult:
        self.entries.append((intent, stop))
        if not self.ok:
            return LiveResult(False, error="insufficient_margin")
        return LiveResult(True, fill=Fill(80_000.0, intent.contracts, 1.0, 0.04), order_id=101)

    async def place_exit(self, pos: Position, intent: OrderIntent) -> LiveResult:
        self.exits.append((pos, intent))
        return LiveResult(True, fill=Fill(80_500.0, pos.contracts, 2.0, 0.04), order_id=102)


def _intent(
    coid: str = "sig-1",
    purpose: Purpose = Purpose.ENTRY,
    symbol: str = "BTCUSD",
    contracts: int = 1,
) -> OrderIntent:
    return OrderIntent(
        strategy="CAN2",
        symbol=symbol,
        side=OrderSide.BUY,
        contracts=contracts,
        purpose=purpose,
        signal_id="sig-1",
        client_order_id=coid,
    )


def _ctx(**kw: Any) -> LiveContext:
    base = {"balance": 25.0, "open_positions": 0, "day_pnl_usd": 0.0, "price": 80_000.0}
    base.update(kw)
    return LiveContext(**base)


def _gw(
    mode: Mode = Mode.LIVE_CAPPED,
    live: FakeLive | None = None,
    halted: bool = False,
    caps: LiveCaps | None = None,
) -> Gateway:
    return Gateway(
        books={},
        matcher=PaperMatcher(),
        mode=lambda: mode,
        halted=lambda: (halted, "test halt"),
        caps=caps or LiveCaps(),
        live=live,  # type: ignore[arg-type]
    )


def _pos() -> Position:
    return Position(
        id="pos-1",
        strategy="CAN2",
        symbol="BTCUSD",
        side=PosSide.LONG,
        contracts=1,
        entry=80_000.0,
        stop=79_000.0,
        initial_stop=79_000.0,
        opened_ts=0.0,
        signal_id="s",
        contract_value=0.001,
        r_unit=1000.0,
    )


def test_live_capped_sizing_rule() -> None:
    caps = LiveCaps(max_contracts=1, leverage=5.0)
    # risk says 0 → still one contract when it fits ($80 notional vs $25 × 5 = $125)
    assert live_capped_contracts(0, caps, balance=25.0, price=80_000, contract_value=0.001) == (
        1,
        "ok",
    )
    assert live_capped_contracts(7, caps, balance=25.0, price=80_000, contract_value=0.001) == (
        1,
        "ok",
    )
    # ETH at $4000 × 0.01 = $40: fits; SOL-sized $110 contract on $20 × 5 = $100: does not
    assert live_capped_contracts(3, caps, balance=25.0, price=4_000, contract_value=0.01) == (
        1,
        "ok",
    )
    n, why = live_capped_contracts(1, caps, balance=20.0, price=110.0, contract_value=1)
    assert n == 0 and "exceeds balance" in why
    assert live_capped_contracts(1, caps, balance=0.0, price=80_000, contract_value=0.001)[0] == 0
    assert live_capped_contracts(
        5, LiveCaps(max_contracts=3, leverage=50), balance=25, price=80_000, contract_value=0.001
    ) == (3, "ok")


def test_caps_each_have_a_reason_and_exits_are_never_capped() -> None:
    gw = _gw(
        caps=LiveCaps(
            symbols=("BTCUSD",),
            max_contracts=1,
            max_positions=2,
            max_orders_per_day=2,
            daily_notional_usd=500,
            daily_loss_usd=3.0,
            leverage=5.0,
        )
    )
    chk = lambda i, c: gw.check_live_caps(i, c, contract_value=0.001)  # noqa: E731
    assert chk(_intent(), _ctx()) is None
    assert "whitelist" in chk(_intent(symbol="SOLUSD"), _ctx())
    assert "contracts > cap" in chk(_intent(contracts=2), _ctx())
    assert "positions open" in chk(_intent(), _ctx(open_positions=2))
    assert "daily loss" in chk(_intent(), _ctx(day_pnl_usd=-3.0))
    assert "exceeds balance" in chk(_intent(), _ctx(balance=10.0))  # $80 > $10 × 5
    gw.orders_today = 2
    assert "orders today" in chk(_intent(), _ctx())
    gw.orders_today, gw.notional_today = 0, 490.0
    assert "daily notional" in chk(_intent(), _ctx())
    # an exit breaks every cap and is still allowed
    gw.orders_today = 99
    assert (
        chk(
            _intent(purpose=Purpose.EXIT, symbol="SOLUSD", contracts=9),
            _ctx(balance=0.0, open_positions=9, day_pnl_usd=-99),
        )
        is None
    )


async def test_sync_submit_refuses_live_modes() -> None:
    r = _gw(Mode.LIVE_CAPPED, FakeLive()).submit(_intent(), contract_value=0.001)
    assert r.decision is Decision.REJECTED_VENUE and "submit_live" in r.order.note


async def test_submit_live_entry_is_submitted_only_on_fill() -> None:
    live = FakeLive()
    gw = _gw(Mode.LIVE_CAPPED, live)
    r = await gw.submit_live(_intent(), contract_value=0.001, ctx=_ctx(), stop_price=79_000.0)
    assert r.decision is Decision.SUBMITTED and r.filled and r.fill is not None
    assert (
        r.order.status == "FILLED"
        and r.order.avg_price == 80_000.0
        and r.order.mode == "LIVE_CAPPED"
    )
    assert "venue order 101" in r.order.note
    assert live.entries[0][1] == 79_000.0
    assert gw.orders_today == 1 and abs(gw.notional_today - 80.0) < 1e-9

    dup = await gw.submit_live(_intent(), contract_value=0.001, ctx=_ctx(), stop_price=79_000.0)
    assert dup.decision is Decision.DUP_BLOCKED and len(live.entries) == 1

    bad = _gw(Mode.LIVE_CAPPED, FakeLive(ok=False))
    r = await bad.submit_live(
        _intent("sig-2"), contract_value=0.001, ctx=_ctx(), stop_price=79_000.0
    )
    assert r.decision is Decision.REJECTED_VENUE and "insufficient_margin" in r.order.note
    assert not r.filled and bad.orders_today == 0 and bad.consecutive_rejects == 1


async def test_submit_live_guards() -> None:
    live = FakeLive()
    # not a live mode
    r = await _gw(Mode.PAPER, live).submit_live(
        _intent(), contract_value=0.001, ctx=_ctx(), stop_price=79_000.0
    )
    assert r.decision is Decision.REJECTED_VENUE and "not in a live mode" in r.order.note
    # no executor (no API keys)
    r = await _gw(Mode.LIVE_CAPPED, None).submit_live(
        _intent(), contract_value=0.001, ctx=_ctx(), stop_price=79_000.0
    )
    assert r.decision is Decision.REJECTED_VENUE and "no API keys" in r.order.note
    # entry without a stop never reaches the venue
    r = await _gw(Mode.LIVE_CAPPED, live).submit_live(_intent(), contract_value=0.001, ctx=_ctx())
    assert r.decision is Decision.REJECTED_RISK and live.entries == []
    # halted blocks entries, cap rejections name the cap
    r = await _gw(Mode.LIVE_CAPPED, live, halted=True).submit_live(
        _intent(), contract_value=0.001, ctx=_ctx(), stop_price=1.0
    )
    assert r.decision is Decision.REJECTED_HALT
    r = await _gw(Mode.LIVE_CAPPED, live).submit_live(
        _intent(symbol="SOLUSD"), contract_value=1, ctx=_ctx(), stop_price=1.0
    )
    assert r.decision is Decision.REJECTED_CAP and "whitelist" in r.order.note
    assert live.entries == []


async def test_live_mode_skips_caps_but_still_needs_stop_and_executor() -> None:
    live = FakeLive()
    gw = _gw(Mode.LIVE, live)
    r = await gw.submit_live(
        _intent(symbol="SOLUSD", contracts=5),
        contract_value=1,
        ctx=_ctx(balance=0.0),
        stop_price=1.0,
    )
    assert r.decision is Decision.SUBMITTED and r.order.mode == "LIVE"


async def test_submit_live_exit_needs_position_and_bypasses_caps() -> None:
    live = FakeLive()
    gw = _gw(Mode.LIVE_CAPPED, live)
    gw.orders_today = 99
    r = await gw.submit_live(
        _intent("x", Purpose.EXIT), contract_value=0.001, ctx=_ctx(balance=0.0)
    )
    assert r.decision is Decision.REJECTED_RISK and "needs the position" in r.order.note
    r = await gw.submit_live(
        _intent("y", Purpose.EXIT), contract_value=0.001, ctx=_ctx(balance=0.0), position=_pos()
    )
    assert r.decision is Decision.SUBMITTED and live.exits[0][0].id == "pos-1"


def test_stats_expose_caps_and_live_configuration() -> None:
    s = _gw(Mode.LIVE_CAPPED, FakeLive()).stats()
    assert s["live_configured"] is True and s["caps"]["max_contracts"] == 1
    assert s["caps"]["symbols"] == ["BTCUSD", "ETHUSD"] and s["caps"]["leverage"] == 5.0
    assert _gw(Mode.PAPER).stats()["live_configured"] is False
