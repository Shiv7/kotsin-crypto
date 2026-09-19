"""Reconciler (R10) against a stub engine: adopt / close / resize, wallet sync, halt on mismatch and
resume on the next clean pass, fetch failures never halt."""

from __future__ import annotations

import asyncio
from typing import Any

from kotsin_crypto.domain import Position, PosSide
from kotsin_crypto.exec.reconcile import Reconciler, wallet_balance_from
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
    }
)


class FakeRest:
    def __init__(self) -> None:
        self.positions_reply: list[dict[str, Any]] = []
        self.orders_reply: list[dict[str, Any]] = []
        self.balances_reply: list[dict[str, Any]] = [{"asset_symbol": "USD", "balance": "25.5"}]
        self.error: Exception | None = None

    async def positions(self) -> list[dict[str, Any]]:
        if self.error:
            raise self.error
        return self.positions_reply

    async def open_orders(self) -> list[dict[str, Any]]:
        return self.orders_reply

    async def wallet_balances(self) -> list[dict[str, Any]]:
        return self.balances_reply


class FakeWallet:
    def __init__(self, balance: float) -> None:
        self.strategy = "CAN2"
        self.balance = balance
        self.peak = balance
        self.updated_ts = 0.0

    def to_json(self) -> dict[str, Any]:
        return {"strategy": self.strategy, "balance": self.balance}


class FakeLedger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.positions: list[dict] = []
        self.trades: list[dict] = []
        self.wallets: list[dict] = []

    async def event(self, kind: str, data: dict) -> None:
        self.events.append((kind, data))

    async def upsert_position(self, d: dict) -> None:
        self.positions.append(d)

    async def insert_trade(self, d: dict) -> None:
        self.trades.append(d)

    async def upsert_wallet(self, key: str, d: dict) -> None:
        self.wallets.append(d)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


class StubEngine:
    def __init__(self) -> None:
        self.rest = FakeRest()
        self.ledger = FakeLedger()
        self.telegram = FakeTelegram()
        self.catalogue = Catalogue([BTC])
        self.settings = type("S", (), {"live_leverage": 5.0})()
        self.strategies: list[Any] = []
        self.symbols = ["BTCUSD", "ETHUSD"]
        self.positions: dict[str, Position] = {}
        self.wallets: dict[str, Any] = {"CAN2": FakeWallet(10.0)}
        self.marks = {"BTCUSD": 80_000.0}
        self.last_price: dict[str, float] = {}
        self.control: dict[str, Any] = {"mode": "LIVE_CAPPED", "halted": False, "halt_reason": ""}
        self.queued: list[Any] = []
        self.halts: list[tuple[bool, str]] = []

    def _persist(self, coro: Any) -> None:
        self.queued.append(coro)

    async def drain(self) -> None:
        while self.queued:
            await self.queued.pop(0)

    def is_live_mode(self) -> bool:
        return self.control["mode"] in ("LIVE_CAPPED", "LIVE")

    def atr_hint(self, symbol: str) -> float | None:
        return 200.0

    async def halt_entries(self, reason: str) -> dict[str, Any]:
        self.halts.append((True, reason))
        self.control.update(halted=True, halt_reason=reason)
        return self.control

    async def resume_entries(self, note: str = "") -> dict[str, Any]:
        self.halts.append((False, note))
        self.control.update(halted=False, halt_reason="")
        return self.control


def _local(contracts: int = 1, side: PosSide = PosSide.LONG) -> Position:
    return Position(
        id="pos-1",
        strategy="CAN2",
        symbol="BTCUSD",
        side=side,
        contracts=contracts,
        entry=79_000.0,
        stop=78_000.0,
        initial_stop=78_000.0,
        opened_ts=0.0,
        signal_id="s",
        contract_value=0.001,
        r_unit=1000.0,
    )


def test_wallet_balance_prefers_settling_asset() -> None:
    assert (
        wallet_balance_from(
            [{"asset_symbol": "BTC", "balance": "9"}, {"asset_symbol": "USD", "balance": "25.5"}]
        )
        == 25.5
    )
    # verified row shape (2026-09-20): equity = available_balance + position_margin
    assert (
        wallet_balance_from(
            [
                {"asset_symbol": "INR", "balance": "0", "available_balance": "0"},
                {
                    "asset_symbol": "USD",
                    "balance": "24.166",
                    "available_balance": "20.166",
                    "position_margin": "4",
                    "order_margin": "0",
                },
            ]
        )
        == 24.166
    )
    assert wallet_balance_from([{"asset_symbol": "USDT", "available_balance": "20"}]) == 20.0
    assert wallet_balance_from([]) is None


async def test_clean_pass_syncs_wallet_and_does_not_halt() -> None:
    eng = StubEngine()
    eng.positions["pos-1"] = _local()
    eng.rest.positions_reply = [{"product_symbol": "BTCUSD", "size": 1, "entry_price": "79000"}]
    rep = await Reconciler(eng).reconcile_once()  # type: ignore[arg-type]
    await eng.drain()
    assert rep.ok and rep.venue_positions == {"BTCUSD": 1} and rep.local_positions == {"BTCUSD": 1}
    assert rep.balance == 25.5 and eng.wallets["CAN2"].balance == 25.5
    assert eng.halts == [] and eng.ledger.wallets[-1]["balance"] == 25.5


async def test_venue_position_we_do_not_hold_is_adopted_with_its_stop_and_entries_halt() -> None:
    eng = StubEngine()
    eng.rest.positions_reply = [{"product_symbol": "BTCUSD", "size": -2, "entry_price": "81000"}]
    eng.rest.orders_reply = [
        {"product_symbol": "BTCUSD", "stop_order_type": "stop_loss_order", "stop_price": "82500"}
    ]
    r = Reconciler(eng)  # type: ignore[arg-type]
    rep = await r.reconcile_once()
    await eng.drain()
    assert not rep.ok and rep.adopted == ["BTCUSD -2"]
    pos = next(iter(eng.positions.values()))
    assert pos.side is PosSide.SHORT and pos.contracts == 2 and pos.entry == 81_000.0
    assert pos.stop == 82_500.0 and pos.leverage == 5.0 and pos.signal_id.startswith("adopted-")
    assert eng.halts[-1][0] is True and eng.halts[-1][1].startswith("RECONCILE")
    assert r.halted_by_us and any(t.startswith("RECONCILE adopted") for t in eng.telegram.sent)

    # next pass is clean → the halt we placed is lifted
    rep2 = await r.reconcile_once()
    assert rep2.ok and eng.halts[-1] == (False, "reconcile clean") and not r.halted_by_us


async def test_adopted_without_stop_uses_atr_and_unknown_symbol_is_ignored() -> None:
    eng = StubEngine()
    eng.rest.positions_reply = [
        {"product_symbol": "BTCUSD", "size": 1, "entry_price": "80000"},
        {"product_symbol": "DOGEUSD", "size": 5, "entry_price": "0.1"},
    ]
    rep = await Reconciler(eng).reconcile_once()  # type: ignore[arg-type]
    await eng.drain()
    assert rep.adopted == ["BTCUSD +1"]
    pos = next(iter(eng.positions.values()))
    assert pos.stop == 80_000.0 - 1.5 * 200.0


async def test_local_position_the_venue_lacks_is_closed_as_reconciled() -> None:
    eng = StubEngine()
    eng.positions["pos-1"] = _local()
    rep = await Reconciler(eng).reconcile_once()  # type: ignore[arg-type]
    await eng.drain()
    assert rep.closed == ["BTCUSD pos-1"] and eng.positions == {}
    assert eng.ledger.trades[-1]["exit_reason"] == "RECONCILED"
    assert eng.ledger.trades[-1]["exit"] == 80_000.0  # closed at mark
    assert eng.ledger.positions[-1]["status"] == "CLOSED"


async def test_size_mismatch_adopts_the_venue_size() -> None:
    eng = StubEngine()
    eng.positions["pos-1"] = _local(contracts=1)
    eng.rest.positions_reply = [{"product_symbol": "BTCUSD", "size": 3, "entry_price": "79500"}]
    rep = await Reconciler(eng).reconcile_once()  # type: ignore[arg-type]
    await eng.drain()
    assert rep.resized == ["BTCUSD local +1 → venue +3"]
    assert eng.positions["pos-1"].contracts == 3 and eng.positions["pos-1"].entry == 79_500.0


async def test_closing_position_counts_as_held() -> None:
    eng = StubEngine()
    p = _local()
    p.status = "CLOSING"
    eng.positions["pos-1"] = p
    eng.rest.positions_reply = [{"product_symbol": "BTCUSD", "size": 1, "entry_price": "79000"}]
    rep = await Reconciler(eng).reconcile_once()  # type: ignore[arg-type]
    await eng.drain()
    assert rep.ok and rep.adopted == []


async def test_fetch_failure_reports_error_and_never_halts() -> None:
    eng = StubEngine()
    eng.rest.error = DeltaApiError(401, {"code": "ip_not_whitelisted_for_api_key"})
    r = Reconciler(eng)  # type: ignore[arg-type]
    rep = await r.reconcile_once()
    assert not rep.ok and "ip_not_whitelisted" in (rep.error or "") and eng.halts == []
    assert r.passes == 0 and r.status()["last"]["error"] == rep.error


async def test_run_loop_only_reconciles_in_live_modes() -> None:
    eng = StubEngine()
    eng.control["mode"] = "PAPER"
    r = Reconciler(eng)  # type: ignore[arg-type]
    r.INTERVAL_S = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await asyncio.sleep(0.05)
    assert r.passes == 0
    eng.control["mode"] = "LIVE_CAPPED"
    await asyncio.sleep(0.05)
    stop.set()
    await task
    await eng.drain()
    assert r.passes >= 1
