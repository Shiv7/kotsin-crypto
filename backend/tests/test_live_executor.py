"""LiveExecutor against a scripted fake venue: leverage set + verified before the first order, market
entry carries a bracket stop on mark price, exits cancel stops then go reduce_only, failures come back
as LiveResult (never raised), kill cancels everything then closes everything."""

from __future__ import annotations

from typing import Any

import pytest

from kotsin_crypto.domain import OrderIntent, OrderSide, Position, PosSide, Purpose
from kotsin_crypto.exec.live import LiveExecutor, order_is_dead, order_is_filled
from kotsin_crypto.venue.delta.catalogue import Catalogue, Product
from kotsin_crypto.venue.delta.rest import DeltaApiError

BTC = Product.from_api(
    {
        "id": 27,
        "symbol": "BTCUSD",
        "contract_type": "perpetual_futures",
        "state": "live",
        "tick_size": "0.5",
        "contract_value": "0.001",
        "taker_commission_rate": "0.0005",
    }
)


class FakeRest:
    """Records every call; `orders` scripts what get_order returns per call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.leverage_reply: dict[str, Any] = {"leverage": "5"}
        self.place_reply: dict[str, Any] = {
            "id": 101,
            "state": "open",
            "size": 1,
            "unfilled_size": 1,
        }
        self.place_error: Exception | None = None
        self.poll_replies: list[dict[str, Any]] = []
        self.open: list[dict[str, Any]] = []
        self.fail_cancel_all = False

    async def set_leverage(self, product_id: int, leverage: float) -> dict[str, Any]:
        self.calls.append(("set_leverage", (product_id, leverage)))
        return {"leverage": str(leverage)}

    async def get_leverage(self, product_id: int) -> dict[str, Any]:
        self.calls.append(("get_leverage", product_id))
        return self.leverage_reply

    async def place_order(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("place_order", kw))
        if self.place_error:
            raise self.place_error
        return dict(self.place_reply)

    async def get_order(self, order_id: int) -> dict[str, Any]:
        self.calls.append(("get_order", order_id))
        if self.poll_replies:
            return self.poll_replies.pop(0)
        return {"id": order_id, "state": "open", "size": 1, "unfilled_size": 1}

    async def get_order_by_client_id(self, coid: str) -> dict[str, Any]:
        self.calls.append(("get_order_by_client_id", coid))
        return await self.get_order(0)

    async def open_orders(self, product_ids: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("open_orders", product_ids))
        return list(self.open)

    async def cancel_order(self, order_id: int, product_id: int, coid: str | None = None) -> dict:
        self.calls.append(("cancel_order", (order_id, product_id, coid)))
        return {"id": order_id, "state": "cancelled"}

    async def cancel_all(self, product_id: int | None = None, **f: Any) -> dict[str, Any]:
        self.calls.append(("cancel_all", product_id))
        if self.fail_cancel_all:
            raise DeltaApiError(400, {"code": "boom"})
        return {"success": True}

    async def close_all_positions(self) -> dict[str, Any]:
        self.calls.append(("close_all", None))
        return {"success": True}

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _exec(rest: FakeRest) -> tuple[LiveExecutor, list[tuple[str, dict]], list[str]]:
    audit: list[tuple[str, dict]] = []
    notes: list[str] = []
    ex = LiveExecutor(
        rest,  # type: ignore[arg-type]
        Catalogue([BTC]),
        leverage=5.0,
        audit=lambda k, d: audit.append((k, d)),
        notify=lambda t: notes.append(t),
    )
    ex.FILL_POLL_S = 0.0
    return ex, audit, notes


def _entry(coid: str = "CAN2-BTCUSD-1-L") -> OrderIntent:
    return OrderIntent(
        strategy="CAN2",
        symbol="BTCUSD",
        side=OrderSide.BUY,
        contracts=1,
        purpose=Purpose.ENTRY,
        signal_id=coid,
        client_order_id=coid,
        reason="test",
        ref_price=80_000.0,
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


def test_order_state_helpers() -> None:
    assert order_is_filled({"unfilled_size": 0, "average_fill_price": "80000"})
    assert not order_is_filled({"unfilled_size": 1, "average_fill_price": "80000"})
    assert not order_is_filled({"unfilled_size": 0})
    assert order_is_dead({"state": "cancelled"})
    assert order_is_dead({"state": "open", "cancellation_reason": "insufficient_margin"})
    assert not order_is_dead({"state": "open"})


async def test_entry_sets_leverage_once_then_market_order_with_mark_price_bracket() -> None:
    rest = FakeRest()
    rest.poll_replies = [
        {"id": 101, "state": "open", "size": 1, "unfilled_size": 1},
        {
            "id": 101,
            "state": "closed",
            "size": 1,
            "unfilled_size": 0,
            "average_fill_price": "80010.5",
            "paid_commission": "0.04",
        },
    ]
    ex, audit, notes = _exec(rest)
    res = await ex.place_entry(_entry(), 79_123.2)
    assert res.ok and res.fill is not None and res.order_id == 101
    assert res.fill.price == 80_010.5 and res.fill.contracts == 1 and res.fill.fee == 0.04
    assert rest.names()[:3] == ["set_leverage", "get_leverage", "place_order"]
    kw = rest.calls[2][1]
    assert kw["order_type"] == "market_order" and kw["side"] == "buy" and kw["size"] == 1
    assert kw["bracket_stop_loss_price"] == 79_123.0  # snapped to the 0.5 tick
    assert kw["bracket_stop_trigger_method"] == "mark_price"
    assert kw["client_order_id"] == "CAN2-BTCUSD-1-L"
    assert ex.stats.orders_filled == 1 and ex.stats.leverage_set == {"BTCUSD": 5.0}
    assert [k for k, _ in audit] == ["live_leverage_set", "live_entry_placed", "live_entry_filled"]
    assert notes and notes[-1].startswith("LIVE ENTRY BUY 1 BTCUSD @ 80010.5")

    # second order on the same product: leverage is not touched again
    rest.poll_replies = [{"id": 102, "unfilled_size": 0, "size": 1, "average_fill_price": "1"}]
    await ex.place_entry(_entry("CAN2-BTCUSD-2-L"), 79_000)
    assert rest.names().count("set_leverage") == 1


async def test_leverage_mismatch_refuses_to_place() -> None:
    rest = FakeRest()
    rest.leverage_reply = {"leverage": "200"}
    ex, audit, _ = _exec(rest)
    res = await ex.place_entry(_entry(), 79_000)
    assert not res.ok and "leverage verify failed" in res.error
    assert "place_order" not in rest.names()
    assert any(k == "live_leverage_mismatch" for k, _ in audit)


async def test_venue_rejection_is_a_result_not_an_exception() -> None:
    rest = FakeRest()
    rest.place_error = DeltaApiError(400, {"code": "insufficient_margin"})
    ex, _, notes = _exec(rest)
    res = await ex.place_entry(_entry(), 79_000)
    assert not res.ok and "insufficient_margin" in res.error and res.fill is None
    assert ex.stats.orders_failed == 1 and ex.stats.last_error == res.error
    assert notes[-1].startswith("LIVE ENTRY REJECTED")


async def test_cancelled_order_and_timeout_are_reported() -> None:
    rest = FakeRest()
    rest.poll_replies = [{"id": 101, "state": "cancelled", "cancellation_reason": "self_trade"}]
    ex, _, _ = _exec(rest)
    res = await ex.place_entry(_entry(), 79_000)
    assert not res.ok and "cancelled" in res.error and "self_trade" in res.error

    rest = FakeRest()
    ex, _, _ = _exec(rest)
    ex.FILL_TIMEOUT_S = 0.0
    res = await ex.place_entry(_entry(), 79_000)
    assert not res.ok and "no fill within" in res.error


async def test_ws_order_update_short_circuits_polling() -> None:
    rest = FakeRest()
    ex, _, _ = _exec(rest)
    ex.note_order_update(
        "CAN2-BTCUSD-1-L",
        {"unfilled_size": 0, "size": 1, "average_fill_price": "80000", "state": "closed"},
    )
    res = await ex.place_entry(_entry(), 79_000)
    assert res.ok and res.fill is not None and res.fill.price == 80_000.0
    assert "get_order" not in rest.names()
    # no commission on the snapshot → taker-rate estimate
    assert abs(res.fill.fee - 80_000 * 1 * 0.001 * 0.0005) < 1e-9


async def test_exit_cancels_resting_stops_then_reduce_only_market() -> None:
    rest = FakeRest()
    rest.open = [
        {"id": 7, "product_id": 27, "stop_order_type": "stop_loss_order", "client_order_id": "b"},
        {"id": 8, "product_id": 28},  # another product: untouched
    ]
    rest.poll_replies = [{"id": 101, "unfilled_size": 0, "size": 1, "average_fill_price": "80500"}]
    ex, _, notes = _exec(rest)
    intent = OrderIntent(
        strategy="CAN2",
        symbol="BTCUSD",
        side=OrderSide.SELL,
        contracts=1,
        purpose=Purpose.EXIT,
        signal_id="s",
        client_order_id="pos-1-x",
        reason="STOP: ratchet",
        position_id="pos-1",
    )
    res = await ex.place_exit(_pos(), intent)
    assert res.ok and res.fill is not None and res.fill.price == 80_500.0
    names = rest.names()
    assert names.index("cancel_order") < names.index("place_order")
    assert [c for c in rest.calls if c[0] == "cancel_order"] == [("cancel_order", (7, 27, "b"))]
    kw = next(c for c in rest.calls if c[0] == "place_order")[1]
    assert (
        kw["reduce_only"] is True and kw["side"] == "sell" and "bracket_stop_loss_price" not in kw
    )
    assert "set_leverage" not in names  # exits never touch leverage
    assert notes[-1].startswith("LIVE EXIT LONG 1 BTCUSD @ 80500")


async def test_kill_cancels_all_then_closes_all_and_reports_errors() -> None:
    rest = FakeRest()
    ex, _, notes = _exec(rest)
    res = await ex.kill()
    assert res.ok and rest.names() == ["cancel_all", "close_all"]
    assert notes[-1] == "LIVE KILL executed" and ex.stats.kills == 1

    rest = FakeRest()
    rest.fail_cancel_all = True
    ex, _, notes = _exec(rest)
    res = await ex.kill()
    assert not res.ok and "cancel_all" in res.error
    assert rest.names() == ["cancel_all", "close_all"]  # close_all still attempted


async def test_audit_failures_never_break_execution() -> None:
    rest = FakeRest()
    rest.poll_replies = [{"id": 101, "unfilled_size": 0, "size": 1, "average_fill_price": "80000"}]

    def bad_audit(kind: str, data: dict) -> None:
        raise RuntimeError("db down")

    ex = LiveExecutor(rest, Catalogue([BTC]), leverage=5.0, audit=bad_audit)  # type: ignore[arg-type]
    ex.FILL_POLL_S = 0.0
    res = await ex.place_entry(_entry(), 79_000)
    assert res.ok


@pytest.mark.parametrize("bad", [None, "", "abc"])
def test_status_is_json_friendly(bad: Any) -> None:
    ex, _, _ = _exec(FakeRest())
    ex.stats.last_error = bad or ""
    s = ex.status()
    assert s["leverage"] == 5.0 and s["orders_placed"] == 0 and isinstance(s["leverage_set"], dict)


async def test_prices_round_to_the_environments_own_tick_and_ids_come_from_the_catalogue() -> None:
    """Testnet BTCUSD is id 84 / tick 0.1 (mainnet: id 27 / tick 0.5) — nothing may be hardcoded.
    The leverage reply shape is the verified one (leverage is a STRING)."""
    testnet_btc = Product.from_api(
        {
            "id": 84,
            "symbol": "BTCUSD",
            "contract_type": "perpetual_futures",
            "state": "live",
            "tick_size": "0.1",
            "contract_value": "0.001",
        }
    )
    rest = FakeRest()
    rest.leverage_reply = {
        "user_id": 1,
        "product_id": 84,
        "leverage": "5",
        "order_margin": "0",
        "leverage_type": "contract",
        "index_symbol": None,
    }
    rest.poll_replies = [{"id": 5, "unfilled_size": 0, "size": 1, "average_fill_price": "81000"}]
    ex = LiveExecutor(rest, Catalogue([testnet_btc]), leverage=5.0)  # type: ignore[arg-type]
    ex.FILL_POLL_S = 0.0
    res = await ex.place_entry(_entry(), 80_123.26)
    assert res.ok
    assert rest.calls[0] == ("set_leverage", (84, 5.0)) and rest.calls[1] == ("get_leverage", 84)
    kw = next(c for c in rest.calls if c[0] == "place_order")[1]
    assert kw["product_id"] == 84 and kw["bracket_stop_loss_price"] == 80_123.3


async def test_venue_default_200x_is_replaced_before_the_first_order() -> None:
    """Mainnet BTCUSD sits at the 200× default: set_leverage must run and be re-read as 5."""
    rest = FakeRest()
    rest.leverage_reply = {"leverage": "5", "leverage_type": "contract"}
    rest.poll_replies = [{"id": 5, "unfilled_size": 0, "size": 1, "average_fill_price": "81000"}]
    ex, _, _ = _exec(rest)
    assert (await ex.ensure_leverage("BTCUSD")).ok
    assert rest.names() == ["set_leverage", "get_leverage"]
    assert rest.calls[0][1] == (27, 5.0)
