from __future__ import annotations

from kotsin_crypto.domain import ExitReason, Position, PosSide
from kotsin_crypto.risk.exits import ExitEngine
from kotsin_crypto.risk.limits import RiskLimits
from kotsin_crypto.risk.sizing import size_position
from kotsin_crypto.risk.wallet import Wallet

LIM = RiskLimits()


def test_sizing_from_risk_budget_and_leverage_cap() -> None:
    # $10k wallet, 0.5% risk = $50; BTC contract 0.001; entry 80000 stop 79600 → $0.4 risk/contract → 125 contracts
    r = size_position(
        balance=10_000,
        entry=80_000,
        stop=79_600,
        contract_value=0.001,
        maintenance_margin_pct=0.25,
        atr=200,
        limits=LIM,
    )
    assert r.ok and r.contracts == 125 and abs(r.risk_usd - 50) < 1e-9
    # but leverage cap 3× → notional ≤ $30k → 375 contracts, so risk binds here; tighten the cap:
    r2 = size_position(
        balance=10_000,
        entry=80_000,
        stop=79_600,
        contract_value=0.001,
        maintenance_margin_pct=0.25,
        atr=200,
        limits=RiskLimits(max_leverage=0.5),
    )
    assert r2.contracts == 62 and r2.leverage <= 0.5
    # risk budget below one contract
    r3 = size_position(
        balance=100,
        entry=80_000,
        stop=79_000,
        contract_value=0.001,
        maintenance_margin_pct=0.25,
        atr=200,
        limits=LIM,
    )
    assert not r3.ok and "1 contract" in r3.reason


def test_wallet_accounting_and_breakers() -> None:
    w = Wallet.new("CAN2", 10_000, now=1_700_000_000)
    w.apply_fee(5, 1_700_000_001)
    w.apply_close(-150, 1_700_000_002)
    assert w.balance == 9845 and w.trades == 1 and w.losses == 1
    assert w.check_breakers(LIM, 1_700_000_003) is None
    w.apply_close(-60, 1_700_000_004)  # day pnl -215 = -2.15%
    assert w.check_breakers(LIM, 1_700_000_005).startswith("DAILY_LOSS")
    assert w.halted
    assert w.rollover(1_700_000_005 + 86_400) and not w.halted and w.day_start_balance == w.balance
    assert Wallet.from_json(w.to_json()) == w


def _pos(side: PosSide) -> Position:
    return Position(
        id="p1",
        strategy="CAN2",
        symbol="BTCUSD",
        side=side,
        contracts=10,
        entry=100.0,
        stop=98.0 if side is PosSide.LONG else 102.0,
        initial_stop=98.0 if side is PosSide.LONG else 102.0,
        opened_ts=0.0,
        signal_id="s",
        contract_value=0.001,
        r_unit=2.0,
    )


def test_exit_engine_ratchet_and_stop() -> None:
    ex = ExitEngine(LIM)
    p = _pos(PosSide.LONG)
    assert ex.on_mark(p, 101.0, 1.0) is None and p.stop == 98.0
    assert ex.on_mark(p, 102.2, 2.0) is None and p.stop == 100.0  # +1.1R → breakeven
    assert ex.on_mark(p, 104.5, 3.0) is None and p.stop == 102.0  # +2.25R → lock +1R
    assert (
        ex.on_mark(p, 112.0, 4.0) is None and abs(p.stop - (100 + (6.0 - 1.5) * 2)) < 1e-9
    )  # trail 1.5R behind 6R
    d = ex.on_mark(p, 108.0, 5.0)
    assert d is not None and d.reason is ExitReason.STOP and p.mfe_r == 6.0
    s = _pos(PosSide.SHORT)
    assert ex.on_mark(s, 95.0, 1.0) is None and s.stop == 98.0  # 2.5R → lock +1R (stop at entry-1R)
    assert ex.on_mark(s, 98.5, 2.0).reason is ExitReason.STOP


def test_time_stop() -> None:
    ex = ExitEngine(RiskLimits(time_stop_s=100))
    p = _pos(PosSide.LONG)
    assert ex.on_clock(p, 100.0, 50.0) is None
    assert ex.on_clock(p, 100.0, 101.0).reason is ExitReason.TIME_STOP
