"""Reconciliation (R10): the exchange is the source of truth. On boot and every 30 s in LIVE modes,
compare Delta's margined positions and open orders with the engine's positions:

* venue position we don't hold locally → adopt it (entry from venue, stop from its resting stop order
  if any, else entry ∓ 1.5×ATR), alert;
* local position the venue no longer has → mark it CLOSED at mark (RECONCILED), alert;
* size mismatch → adopt the venue size, alert;
* any mismatch → halt new entries with reason RECONCILE until a clean pass;
* wallet balance := venue USD/USDT wallet balance.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from ..domain import ExitReason, Position, PosSide, Trade, new_id, to_json
from ..venue.delta.rest import DeltaApiError

log = structlog.get_logger("reconcile")

SETTLING_ASSETS = ("USD", "USDT")


class EngineLike(Protocol):
    """The slice of the engine the reconciler touches (a Protocol keeps exec free of engine/strategy
    imports and lets tests pass a stub)."""

    rest: Any
    ledger: Any
    telegram: Any
    catalogue: Any
    settings: Any
    strategies: Any
    symbols: list[str]
    positions: dict[str, Position]
    wallets: dict[str, Any]
    marks: dict[str, float]
    last_price: dict[str, float]
    control: dict[str, Any]

    def _persist(self, coro: Any) -> None: ...
    def is_live_mode(self) -> bool: ...
    def atr_hint(self, symbol: str) -> float | None: ...
    async def halt_entries(self, reason: str) -> Any: ...
    async def resume_entries(self, note: str = "") -> Any: ...


@dataclass(slots=True)
class ReconcileReport:
    ts: float
    ok: bool = True
    adopted: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    resized: list[str] = field(default_factory=list)
    venue_positions: dict[str, int] = field(default_factory=dict)
    local_positions: dict[str, int] = field(default_factory=dict)
    open_orders: int = 0
    balance: float | None = None
    rebaselined: bool = False
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "ok": self.ok,
            "adopted": list(self.adopted),
            "closed": list(self.closed),
            "resized": list(self.resized),
            "venue_positions": dict(self.venue_positions),
            "local_positions": dict(self.local_positions),
            "open_orders": self.open_orders,
            "balance": self.balance,
            "rebaselined": self.rebaselined,
            "error": self.error,
        }


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def wallet_balance_from(balances: list[dict[str, Any]]) -> float | None:
    """Equity from the settling-asset row (USD on Delta India; verified 2026-09-20: rows look like
    ``{"asset_symbol":"USD","balance":"24.166","available_balance":"24.166","position_margin":"0",
    "order_margin":"0",...}`` next to zero INR/BTC/ETH/SOL/XRP rows). Equity = available_balance +
    position_margin (order margin for a resting reduce-only stop is not ours to count); ``balance``
    only when the row lacks the split."""
    rows = [b for b in balances if str(b.get("asset_symbol", "")).upper() in SETTLING_ASSETS]
    if not rows:
        rows = sorted(balances, key=lambda b: -_f(b.get("balance")))[:1]
    if not rows:
        return None
    b = rows[0]
    if b.get("available_balance") not in (None, ""):
        return _f(b.get("available_balance")) + _f(b.get("position_margin"))
    return _f(b.get("balance")) if b.get("balance") not in (None, "") else None


class Reconciler:
    INTERVAL_S = 30.0

    def __init__(self, engine: EngineLike) -> None:
        self.engine = engine
        self.last: ReconcileReport | None = None
        self.passes = 0
        self.mismatches = 0
        self.halted_by_us = False

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.INTERVAL_S)
                break
            except TimeoutError:
                pass
            if self.engine.is_live_mode():
                try:
                    await self.reconcile_once()
                except Exception:
                    log.exception("reconcile_failed")

    async def reconcile_once(self) -> ReconcileReport:
        eng = self.engine
        rep = ReconcileReport(ts=time.time())
        try:
            venue_raw = await eng.rest.positions()
            orders = await eng.rest.open_orders()
            balances = await eng.rest.wallet_balances()
        except DeltaApiError as exc:
            rep.ok = False
            rep.error = str(exc)
            self.last = rep
            log.error("reconcile_fetch_failed", error=str(exc))
            return rep
        self.passes += 1
        rep.open_orders = len(orders)
        rep.balance = wallet_balance_from(balances)

        venue: dict[str, dict[str, Any]] = {}
        for p in venue_raw:
            sym = str(p.get("product_symbol") or (p.get("product") or {}).get("symbol") or "")
            size = int(_f(p.get("size")))
            if sym and size != 0:
                venue[sym] = p
                rep.venue_positions[sym] = size
        local: dict[str, list[Position]] = {}
        for pos in eng.positions.values():
            if pos.status in ("OPEN", "CLOSING"):
                local.setdefault(pos.symbol, []).append(pos)
                rep.local_positions[pos.symbol] = (
                    rep.local_positions.get(pos.symbol, 0) + pos.contracts * pos.direction
                )
        stops_by_symbol: dict[str, float] = {}
        for o in orders:
            sym = str(o.get("product_symbol") or "")
            if o.get("stop_order_type") == "stop_loss_order" and o.get("stop_price") not in (
                None,
                "",
            ):
                stops_by_symbol[sym] = _f(o.get("stop_price"))

        now = time.time()
        for sym, p in venue.items():
            vsize = int(_f(p.get("size")))
            lsize = rep.local_positions.get(sym, 0)
            if sym not in local:
                if sym not in eng.symbols:
                    log.warning("reconcile_unknown_symbol_position", symbol=sym, size=vsize)
                    continue
                self._adopt(sym, p, stops_by_symbol.get(sym), now)
                rep.adopted.append(f"{sym} {vsize:+d}")
            elif vsize != lsize:
                pos = local[sym][0]
                pos.contracts = abs(vsize)
                pos.side = PosSide.LONG if vsize > 0 else PosSide.SHORT
                pos.entry = _f(p.get("entry_price"), pos.entry)
                eng._persist(eng.ledger.upsert_position(to_json(pos)))
                rep.resized.append(f"{sym} local {lsize:+d} → venue {vsize:+d}")
        for sym, poss in local.items():
            if sym not in venue:
                for pos in poss:
                    self._close_locally(pos, now)
                    rep.closed.append(f"{sym} {pos.id}")
        if rep.balance is not None:
            for w in eng.wallets.values():
                if not w.venue_synced:
                    # first sync: the paper wallet's $10k baseline must not be compared with the
                    # venue's real balance, or the daily-loss cap trips on a phantom −$9,900 day
                    w.rebaseline(rep.balance, time.time())
                    rep.rebaselined = True
                    w.updated_ts = now
                    eng._persist(eng.ledger.upsert_wallet(w.strategy, w.to_json()))
                elif abs(w.balance - rep.balance) > 1e-6:
                    w.balance = rep.balance
                    w.peak = max(w.peak, w.balance)
                    w.updated_ts = now
                    eng._persist(eng.ledger.upsert_wallet(w.strategy, w.to_json()))

        mismatch = bool(rep.adopted or rep.closed or rep.resized)
        rep.ok = not mismatch
        self.last = rep
        if mismatch:
            self.mismatches += 1
            detail = "; ".join(rep.adopted + rep.closed + rep.resized)
            log.warning("reconcile_mismatch", detail=detail)
            eng._persist(eng.ledger.event("reconcile", rep.to_json()))
            reason = str(eng.control.get("halt_reason") or "")
            if eng.control.get("halted") and not reason.startswith("RECONCILE"):
                # An operator halt (manual / kill) outranks ours: never replace it with one we
                # would lift automatically on the next clean pass.
                eng._persist(
                    eng.telegram.send(f"RECONCILE mismatch (already halted): {detail[:200]}")
                )
            else:
                self.halted_by_us = True
                await eng.halt_entries(f"RECONCILE: {detail[:120]}")
                eng._persist(
                    eng.telegram.send(f"RECONCILE mismatch — entries halted: {detail[:200]}")
                )
        elif self.halted_by_us and str(eng.control.get("halt_reason", "")).startswith("RECONCILE"):
            self.halted_by_us = False
            await eng.resume_entries("reconcile clean")
        return rep

    def _adopt(self, symbol: str, p: dict[str, Any], stop: float | None, now: float) -> Position:
        eng = self.engine
        size = int(_f(p.get("size")))
        side = PosSide.LONG if size > 0 else PosSide.SHORT
        entry = (
            _f(p.get("entry_price")) or eng.marks.get(symbol) or eng.last_price.get(symbol) or 0.0
        )
        assert eng.catalogue is not None
        product = eng.catalogue.by_symbol(symbol)
        if stop is None:
            atr = eng.atr_hint(symbol) or entry * 0.003
            stop = entry - 1.5 * atr if side is PosSide.LONG else entry + 1.5 * atr
        dist = abs(entry - stop) or entry * 0.003
        pos = Position(
            id=new_id("pos"),
            strategy=eng.strategies[0].key.value if eng.strategies else "MANUAL",
            symbol=symbol,
            side=side,
            contracts=abs(size),
            entry=entry,
            stop=stop,
            initial_stop=stop,
            opened_ts=now,
            signal_id=f"adopted-{int(now)}",
            contract_value=float(product.contract_value),
            r_unit=dist,
            notional=entry * abs(size) * float(product.contract_value),
            leverage=eng.settings.live_leverage,
        )
        eng.positions[pos.id] = pos
        eng._persist(eng.ledger.upsert_position(to_json(pos)))
        eng._persist(
            eng.telegram.send(
                f"RECONCILE adopted venue position {side.value} {abs(size)} {symbol} @ "
                f"{entry:.6g} stop {stop:.6g}"
            )
        )
        log.warning("reconcile_adopted", symbol=symbol, size=size, entry=entry, stop=stop)
        return pos

    def _close_locally(self, pos: Position, now: float) -> None:
        eng = self.engine
        mark = eng.marks.get(pos.symbol) or eng.last_price.get(pos.symbol) or pos.entry
        pnl = pos.unrealized(mark)
        pos.status, pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl = (
            "CLOSED",
            now,
            mark,
            ExitReason.MANUAL.value,
            pnl,
        )
        net = pnl - pos.fees - pos.funding
        trade = Trade(
            id=new_id("trd"),
            position_id=pos.id,
            strategy=pos.strategy,
            symbol=pos.symbol,
            side=pos.side,
            contracts=pos.contracts,
            entry=pos.entry,
            exit=mark,
            pnl=pnl,
            fees=pos.fees,
            funding=pos.funding,
            net=net,
            r_multiple=(mark - pos.entry) * pos.direction / pos.r_unit if pos.r_unit else 0.0,
            mfe_r=pos.mfe_r,
            mae_r=pos.mae_r,
            exit_reason="RECONCILED",
            opened_ts=pos.opened_ts,
            closed_ts=now,
            duration_s=now - pos.opened_ts,
            signal_id=pos.signal_id,
        )
        eng.positions.pop(pos.id, None)
        eng._persist(eng.ledger.upsert_position(to_json(pos)))
        eng._persist(eng.ledger.insert_trade(to_json(trade)))
        eng._persist(
            eng.telegram.send(
                f"RECONCILE closed local {pos.side.value} {pos.contracts} {pos.symbol} "
                f"(venue has no position) at {mark:.6g}"
            )
        )
        log.warning("reconcile_closed_local", symbol=pos.symbol, position_id=pos.id)

    def status(self) -> dict[str, Any]:
        return {
            "passes": self.passes,
            "mismatches": self.mismatches,
            "halted_by_us": self.halted_by_us,
            "last": self.last.to_json() if self.last else None,
        }
