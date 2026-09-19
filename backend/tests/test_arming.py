"""Arming (R9) and the live wiring at engine level, with fakes in place of the venue: LIVE modes need
keys + an executor + an expiry, expiry drops to PAPER without flattening, boot with a stale arm boots
into PAPER, kill halts then hands over to the executor, live entries go through submit_live and mirror
the venue bracket stop."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kotsin_crypto.bus import Bus
from kotsin_crypto.config import DeltaEnv, Settings
from kotsin_crypto.domain import Fill, OrderIntent, Position, PosSide
from kotsin_crypto.engine import Engine
from kotsin_crypto.exec.gateway import Gateway
from kotsin_crypto.exec.live import LiveResult
from kotsin_crypto.ledger import db as ledgerdb
from kotsin_crypto.ops.telegram import Telegram
from kotsin_crypto.risk.wallet import Wallet
from kotsin_crypto.strategy.base import Side, Signal
from kotsin_crypto.strategy.keys import StrategyKey
from kotsin_crypto.venue.delta.catalogue import Catalogue, Product

BTC = Product.from_api(
    {
        "id": 27,
        "symbol": "BTCUSD",
        "contract_type": "perpetual_futures",
        "state": "live",
        "tick_size": "0.5",
        "contract_value": "0.001",
        "maintenance_margin": "0.5",
        "taker_commission_rate": "0.0005",
    }
)


class FakeRest:
    """Only what the reconciler and Engine.stop touch."""

    def __init__(self) -> None:
        self.positions_reply: list[dict[str, Any]] = []
        self.budget = type("B", (), {"used": lambda self: 0, "quota": 10_000})()

    async def positions(self) -> list[dict[str, Any]]:
        return self.positions_reply

    async def open_orders(self) -> list[dict[str, Any]]:
        return []

    async def wallet_balances(self) -> list[dict[str, Any]]:
        return [{"asset_symbol": "USD", "balance": "25"}]

    async def aclose(self) -> None:
        pass


class FakeLive:
    def __init__(self) -> None:
        self.entries: list[tuple[OrderIntent, float]] = []
        self.exits: list[OrderIntent] = []
        self.killed = 0
        self.exit_ok = True

    async def place_entry(self, intent: OrderIntent, stop: float) -> LiveResult:
        self.entries.append((intent, stop))
        return LiveResult(
            True, fill=Fill(80_020.0, intent.contracts, time.time(), 0.04), order_id=101
        )

    async def place_exit(self, pos: Position, intent: OrderIntent) -> LiveResult:
        self.exits.append(intent)
        if not self.exit_ok:
            return LiveResult(False, error="venue down")
        return LiveResult(True, fill=Fill(80_500.0, pos.contracts, time.time(), 0.04), order_id=102)

    async def kill(self) -> LiveResult:
        self.killed += 1
        return LiveResult(True)

    def status(self) -> dict[str, Any]:
        return {"fake": True}


@pytest.fixture
async def eng(tmp_path: Path):
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        delta_env=DeltaEnv.TESTNET,
        engine_enabled=False,
        symbols="BTCUSD",
        delta_api_key="k",
        delta_api_secret="s",
        data_dir=tmp_path,
        db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db",
    )
    e = Engine(settings, Bus(), Telegram(settings))
    await e.rest.aclose()
    e.rest = FakeRest()  # type: ignore[assignment]
    await e.ledger.init()
    e.control = await e.ledger.get_control()
    e.catalogue = Catalogue([BTC])
    e.wallets["CAN2"] = Wallet.new("CAN2", 25.0)
    e.marks["BTCUSD"] = 80_000.0
    yield e
    await e.drain_for_test()
    await e.ledger.close()


async def _drain(e: Engine) -> None:
    while not e._db_queue.empty():
        await e._db_queue.get_nowait()


Engine.drain_for_test = _drain  # type: ignore[attr-defined]


def _arm(e: Engine, live: FakeLive | None = None) -> FakeLive:
    live = live or FakeLive()
    e.live = live  # type: ignore[assignment]
    e.gateway = Gateway(
        books=e.books,
        matcher=e.matcher,
        mode=e._mode,
        halted=e._halted,
        caps=e.live_caps,
        live=live,  # type: ignore[arg-type]
    )
    return live


def _signal(ts: int = 1_700_000_000) -> Signal:
    return Signal(
        strategy=StrategyKey.CAN2,
        symbol="BTCUSD",
        side=Side.LONG,
        ts=ts,
        entry=Decimal("80000"),
        stop=Decimal("79000"),
        reason="test",
        evidence={"atr": 500.0},
    )


async def test_live_modes_require_keys_executor_and_an_expiry(eng: Engine, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="KC_DELTA_API_KEY"):
        await eng.set_mode("LIVE_CAPPED", arm_hours=8)  # keys set but no executor yet
    _arm(eng)
    with pytest.raises(ValueError, match="arm_hours"):
        await eng.set_mode("LIVE_CAPPED")
    with pytest.raises(ValueError, match="arm_hours"):
        await eng.set_mode("LIVE", arm_hours=48)
    assert eng.control["mode"] == "SHADOW" and not eng.live_armed()

    nokeys = Settings(  # type: ignore[call-arg]
        _env_file=None, delta_env=DeltaEnv.TESTNET, engine_enabled=False, data_dir=tmp_path
    )
    e2 = Engine(nokeys, Bus(), Telegram(nokeys))
    await e2.rest.aclose()
    _arm(e2)
    with pytest.raises(ValueError, match="KC_DELTA_API_KEY"):
        await e2.set_mode("LIVE_CAPPED", arm_hours=1)


async def test_arm_sets_expiry_and_expiry_drops_to_paper_without_flattening(eng: Engine) -> None:
    _arm(eng)
    before = time.time()
    ctl = await eng.set_mode("LIVE_CAPPED", arm_hours=8)
    assert ctl["mode"] == "LIVE_CAPPED" and eng.is_live_mode() and eng.live_armed()
    assert abs(ctl["armed_until"] - (before + 8 * 3600)) < 5
    assert eng.reconciler.passes == 1  # arming triggers an immediate reconcile
    assert eng.snapshot(brief=True)["armed"] is True

    eng.positions["pos-1"] = Position(
        id="pos-1",
        strategy="CAN2",
        symbol="BTCUSD",
        side=PosSide.LONG,
        contracts=1,
        entry=80_000.0,
        stop=79_000.0,
        initial_stop=79_000.0,
        opened_ts=time.time(),
        signal_id="s",
        contract_value=0.001,
        r_unit=1000.0,
    )
    eng.marks.clear()  # no reference price → no clock exits in this tick
    eng._tick(time.time() + 9 * 3600)
    await eng.drain_for_test()  # type: ignore[attr-defined]
    assert eng.control["mode"] == "PAPER" and eng.control["armed_until"] is None
    assert (await eng.ledger.get_control())["mode"] == "PAPER"
    assert "pos-1" in eng.positions and eng.positions["pos-1"].status == "OPEN"
    assert not eng.control["halted"]
    events = await eng.ledger.recent(ledgerdb.events, limit=20)
    assert any(e["kind"] == "disarm" for e in events)


async def test_boot_with_stale_arm_or_without_keys_boots_into_paper(eng: Engine) -> None:
    await eng.ledger.set_mode("LIVE_CAPPED", time.time() - 1)
    eng.control = await eng.ledger.get_control()
    _arm(eng)
    assert await eng.check_arm_at_boot() is True
    assert eng.control["mode"] == "PAPER"

    await eng.ledger.set_mode("LIVE", time.time() + 3600)
    eng.control = await eng.ledger.get_control()
    assert await eng.check_arm_at_boot() is False and eng.control["mode"] == "LIVE"

    eng.live = None
    assert await eng.check_arm_at_boot() is True and eng.control["mode"] == "PAPER"


async def test_halt_entries_keeps_positions_and_kill_hands_over_to_the_executor(
    eng: Engine,
) -> None:
    live = _arm(eng)
    await eng.set_mode("LIVE_CAPPED", arm_hours=1)
    pos = Position(
        id="pos-1",
        strategy="CAN2",
        symbol="BTCUSD",
        side=PosSide.LONG,
        contracts=1,
        entry=80_000.0,
        stop=79_000.0,
        initial_stop=79_000.0,
        opened_ts=time.time(),
        signal_id="s",
        contract_value=0.001,
        r_unit=1000.0,
    )
    eng.positions["pos-1"] = pos
    eng.rest.positions_reply = [{"product_symbol": "BTCUSD", "size": 1, "entry_price": "80000"}]  # type: ignore[attr-defined]

    await eng.halt_entries("RECONCILE: test")
    assert eng.control["halted"] and eng.control["halt_reason"] == "RECONCILE: test"
    assert eng.positions["pos-1"].status == "OPEN" and live.exits == []
    await eng.resume_entries("clean")
    assert not eng.control["halted"]

    eng.rest.positions_reply = []  # type: ignore[attr-defined]  # venue flat after close_all
    out = await eng.kill("manual kill (test)")
    await eng.drain_for_test()  # type: ignore[attr-defined]
    assert live.killed == 1 and out["live"] == {"ok": True, "error": ""}
    assert eng.control["halted"] and eng.control["halt_reason"] == "manual kill (test)"
    assert live.exits == []  # nothing flattened through the gateway: the venue did it
    assert out["reconcile"]["closed"] == ["BTCUSD pos-1"] and eng.positions == {}


async def test_live_entry_goes_through_submit_live_and_mirrors_the_venue_stop(eng: Engine) -> None:
    live = _arm(eng)
    await eng.set_mode("LIVE_CAPPED", arm_hours=1)
    sig = _signal()
    eng._on_signal(sig, None, time.time())  # type: ignore[arg-type]
    assert eng._pending_entries == {"BTCUSD"}
    # a second signal on the same symbol while the first is in flight is refused
    eng._on_signal(_signal(ts=1_700_000_300), None, time.time())  # type: ignore[arg-type]
    assert eng.recent_signals[0]["decision_reason"].startswith("live entry already pending")
    await asyncio.gather(*eng._live_tasks)
    await eng.drain_for_test()  # type: ignore[attr-defined]

    assert eng._pending_entries == set()
    intent, stop = live.entries[0]
    assert intent.contracts == 1 and intent.client_order_id == sig.signal_id and stop == 79_000.0
    pos = next(iter(eng.positions.values()))
    assert pos.entry == 80_020.0 and pos.stop == 79_000.0 == pos.initial_stop
    assert pos.r_unit == 1_020.0 and pos.leverage == 5.0 and pos.contracts == 1
    rec = next(r for r in eng.recent_signals if r["signal_id"] == sig.signal_id)
    assert rec["decision"] == "SUBMITTED"
    assert rec["live"]["contracts"] == 1 and rec["live"]["stop"] == 79_000.0
    assert eng.counters["entry_SUBMITTED"] == 1

    # exit: CLOSING while in flight, then CLOSED with a trade
    from kotsin_crypto.domain import ExitDecision, ExitReason

    eng._close_position(pos, ExitDecision(pos.id, ExitReason.MANUAL, 80_500.0, "test"), time.time())
    assert pos.status == "CLOSING"
    eng._close_position(pos, ExitDecision(pos.id, ExitReason.MANUAL, 80_500.0, "dup"), time.time())
    await asyncio.gather(*eng._live_tasks)
    await eng.drain_for_test()  # type: ignore[attr-defined]
    assert len(live.exits) == 1 and live.exits[0].client_order_id == f"{pos.id}-x"[:32]
    assert pos.status == "CLOSED" and eng.positions == {}
    trades = await eng.ledger.recent(ledgerdb.trades, limit=5, order_col="closed_ts")
    assert trades and trades[0]["exit"] == 80_500.0


async def test_failed_live_exit_reverts_to_open(eng: Engine) -> None:
    live = _arm(eng)
    live.exit_ok = False
    await eng.set_mode("LIVE_CAPPED", arm_hours=1)
    pos = Position(
        id="pos-1",
        strategy="CAN2",
        symbol="BTCUSD",
        side=PosSide.LONG,
        contracts=1,
        entry=80_000.0,
        stop=79_000.0,
        initial_stop=79_000.0,
        opened_ts=time.time(),
        signal_id="s",
        contract_value=0.001,
        r_unit=1000.0,
    )
    eng.positions["pos-1"] = pos
    from kotsin_crypto.domain import ExitDecision, ExitReason

    eng._close_position(pos, ExitDecision(pos.id, ExitReason.STOP, 79_000.0, "hit"), time.time())
    await asyncio.gather(*eng._live_tasks)
    await eng.drain_for_test()  # type: ignore[attr-defined]
    assert pos.status == "OPEN" and "pos-1" in eng.positions
    assert eng.counters["exit_failed"] == 1


async def test_tiny_account_is_rejected_by_risk_sizing_in_paper_but_capped_to_one_live(
    eng: Engine,
) -> None:
    await eng.set_mode("PAPER")
    eng._on_signal(_signal(), None, time.time())  # type: ignore[arg-type]
    assert eng.recent_signals[0]["decision_reason"].startswith("sizing:")
    live = _arm(eng)
    await eng.set_mode("LIVE_CAPPED", arm_hours=1)
    eng._on_signal(_signal(ts=1_700_000_600), None, time.time())  # type: ignore[arg-type]
    await asyncio.gather(*eng._live_tasks)
    await eng.drain_for_test()  # type: ignore[attr-defined]
    assert live.entries and live.entries[0][0].contracts == 1
