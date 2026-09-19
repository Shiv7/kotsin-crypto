"""Real orders on Delta, behind the gateway. Every venue action is audited (ledger event + Telegram
via callbacks) and never raises into the engine: outcomes come back as ``LiveResult``.

Entry = MARKET order with a bracket stop-loss attached (``bracket_stop_trigger_method=mark_price``),
so the venue keeps the stop alive even if this process dies. Exit = cancel resting stop orders for the
product, then a ``reduce_only`` MARKET order. Leverage is set and verified per product once per
session before the first order (Delta's default is 200×). ``kill()`` = cancel everything + close every
position at market.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from ..domain import Fill, OrderIntent, Position
from ..venue.delta.catalogue import Catalogue
from ..venue.delta.rest import DeltaApiError, DeltaRest

log = structlog.get_logger("live")

Audit = Callable[[str, dict[str, Any]], Awaitable[None] | None]
Notify = Callable[[str], Awaitable[Any] | None]


@dataclass(slots=True)
class LiveResult:
    ok: bool
    fill: Fill | None = None
    error: str = ""
    venue_order: dict[str, Any] | None = None
    order_id: int | None = None


@dataclass(slots=True)
class LiveStats:
    orders_placed: int = 0
    orders_filled: int = 0
    orders_failed: int = 0
    cancels: int = 0
    kills: int = 0
    leverage_set: dict[str, float] = field(default_factory=dict)
    last_error: str = ""
    last_order_ts: float | None = None


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(float(v)) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def order_is_filled(o: dict[str, Any]) -> bool:
    return _i(o.get("unfilled_size"), -1) == 0 and _f(o.get("average_fill_price")) > 0


def order_is_dead(o: dict[str, Any]) -> bool:
    return str(o.get("state")) == "cancelled" or bool(o.get("cancellation_reason"))


class LiveExecutor:
    FILL_POLL_S = 0.5
    FILL_TIMEOUT_S = 10.0

    def __init__(
        self,
        rest: DeltaRest,
        catalogue: Catalogue,
        *,
        leverage: float,
        taker_fee_rate: float = 0.0005,
        audit: Audit | None = None,
        notify: Notify | None = None,
    ) -> None:
        self.rest = rest
        self.catalogue = catalogue
        self.leverage = leverage
        self.taker_fee_rate = taker_fee_rate
        self._audit = audit
        self._notify = notify
        self.stats = LiveStats()
        self._leverage_ok: set[str] = set()
        self._order_updates: dict[str, dict[str, Any]] = {}  # client_order_id → last WS snapshot

    # ---- plumbing --------------------------------------------------------------------------------
    async def _log(self, kind: str, data: dict[str, Any], text: str | None = None) -> None:
        log.info(f"live_{kind}", **{k: v for k, v in data.items() if k != "raw"})
        if self._audit is not None:
            try:
                r = self._audit(f"live_{kind}", data)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("live_audit_failed")
        if text and self._notify is not None:
            try:
                r = self._notify(text)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("live_notify_failed")

    def note_order_update(self, client_order_id: str | None, snapshot: dict[str, Any]) -> None:
        """Private-WS order updates land here so a fill is recognised without waiting for a poll."""
        if client_order_id:
            self._order_updates[client_order_id] = snapshot

    # ---- leverage --------------------------------------------------------------------------------
    async def ensure_leverage(self, symbol: str) -> LiveResult:
        if symbol in self._leverage_ok:
            return LiveResult(True)
        product = self.catalogue.by_symbol(symbol)
        try:
            await self.rest.set_leverage(product.id, self.leverage)
            got = await self.rest.get_leverage(product.id)
        except DeltaApiError as exc:
            self.stats.last_error = str(exc)
            await self._log(
                "leverage_error",
                {"symbol": symbol, "error": str(exc)},
                f"LIVE leverage error {symbol}: {exc}",
            )
            return LiveResult(False, error=str(exc))
        actual = _f(got.get("leverage"))
        if actual and abs(actual - self.leverage) > 1e-6:
            msg = (
                f"leverage verify failed for {symbol}: wanted {self.leverage}, venue says {actual}"
            )
            self.stats.last_error = msg
            await self._log(
                "leverage_mismatch",
                {"symbol": symbol, "wanted": self.leverage, "actual": actual},
                f"LIVE leverage mismatch {symbol}: {actual}× (wanted {self.leverage}×)",
            )
            return LiveResult(False, error=msg)
        self._leverage_ok.add(symbol)
        self.stats.leverage_set[symbol] = actual or self.leverage
        await self._log("leverage_set", {"symbol": symbol, "leverage": actual or self.leverage})
        return LiveResult(True)

    # ---- orders ------------------------------------------------------------------------------------
    async def place_entry(self, intent: OrderIntent, stop_price: float) -> LiveResult:
        product = self.catalogue.by_symbol(intent.symbol)
        lev = await self.ensure_leverage(intent.symbol)
        if not lev.ok:
            return lev
        stop = float(product.round_price(Decimal(str(stop_price))))
        try:
            self.stats.orders_placed += 1
            self.stats.last_order_ts = time.time()
            order = await self.rest.place_order(
                product_id=product.id,
                size=intent.contracts,
                side=intent.side.value.lower(),
                order_type="market_order",
                client_order_id=intent.client_order_id,
                bracket_stop_loss_price=stop,
                bracket_stop_trigger_method="mark_price",
            )
        except DeltaApiError as exc:
            self.stats.orders_failed += 1
            self.stats.last_error = str(exc)
            await self._log(
                "entry_rejected",
                {
                    "symbol": intent.symbol,
                    "side": intent.side.value,
                    "contracts": intent.contracts,
                    "error": str(exc),
                },
                f"LIVE ENTRY REJECTED {intent.side.value} {intent.contracts} {intent.symbol}: {exc}",
            )
            return LiveResult(False, error=str(exc))
        await self._log(
            "entry_placed",
            {
                "symbol": intent.symbol,
                "side": intent.side.value,
                "contracts": intent.contracts,
                "stop": stop,
                "order_id": order.get("id"),
                "client_order_id": intent.client_order_id,
            },
        )
        res = await self._await_fill(order, intent.client_order_id, float(product.contract_value))
        if res.ok and res.fill:
            self.stats.orders_filled += 1
            await self._log(
                "entry_filled",
                {
                    "symbol": intent.symbol,
                    "side": intent.side.value,
                    "contracts": res.fill.contracts,
                    "price": res.fill.price,
                    "fee": res.fill.fee,
                    "order_id": res.order_id,
                },
                f"LIVE ENTRY {intent.side.value} {res.fill.contracts} {intent.symbol} @ "
                f"{res.fill.price:.6g} stop {stop:.6g} fee {res.fill.fee:.4f}",
            )
        else:
            self.stats.orders_failed += 1
            await self._log(
                "entry_unfilled",
                {"symbol": intent.symbol, "error": res.error, "order_id": res.order_id},
                f"LIVE ENTRY UNFILLED {intent.symbol}: {res.error}",
            )
        return res

    async def place_exit(self, pos: Position, intent: OrderIntent) -> LiveResult:
        product = self.catalogue.by_symbol(pos.symbol)
        await self.cancel_stops(pos.symbol)
        try:
            self.stats.orders_placed += 1
            self.stats.last_order_ts = time.time()
            order = await self.rest.place_order(
                product_id=product.id,
                size=pos.contracts,
                side=intent.side.value.lower(),
                order_type="market_order",
                reduce_only=True,
                client_order_id=intent.client_order_id,
            )
        except DeltaApiError as exc:
            self.stats.orders_failed += 1
            self.stats.last_error = str(exc)
            await self._log(
                "exit_rejected",
                {"symbol": pos.symbol, "position_id": pos.id, "error": str(exc)},
                f"LIVE EXIT REJECTED {pos.symbol}: {exc}",
            )
            return LiveResult(False, error=str(exc))
        res = await self._await_fill(order, intent.client_order_id, float(product.contract_value))
        if res.ok and res.fill:
            self.stats.orders_filled += 1
            await self._log(
                "exit_filled",
                {
                    "symbol": pos.symbol,
                    "position_id": pos.id,
                    "contracts": res.fill.contracts,
                    "price": res.fill.price,
                    "fee": res.fill.fee,
                    "reason": intent.reason,
                },
                f"LIVE EXIT {pos.side.value} {res.fill.contracts} {pos.symbol} @ "
                f"{res.fill.price:.6g} ({intent.reason[:60]})",
            )
        else:
            self.stats.orders_failed += 1
            await self._log(
                "exit_unfilled",
                {"symbol": pos.symbol, "position_id": pos.id, "error": res.error},
                f"LIVE EXIT UNFILLED {pos.symbol}: {res.error}",
            )
        return res

    async def cancel_stops(self, symbol: str) -> int:
        """Cancel resting stop / bracket orders for the product (before a manual exit)."""
        product = self.catalogue.by_symbol(symbol)
        try:
            orders = await self.rest.open_orders(product_ids=str(product.id))
        except DeltaApiError as exc:
            await self._log("cancel_stops_error", {"symbol": symbol, "error": str(exc)})
            return 0
        n = 0
        for o in orders:
            if _i(o.get("product_id")) != product.id:
                continue
            try:
                await self.rest.cancel_order(_i(o.get("id")), product.id, o.get("client_order_id"))
                n += 1
                self.stats.cancels += 1
            except DeltaApiError as exc:
                await self._log(
                    "cancel_error",
                    {"symbol": symbol, "order_id": o.get("id"), "error": str(exc)},
                )
        if n:
            await self._log("stops_cancelled", {"symbol": symbol, "count": n})
        return n

    async def kill(self) -> LiveResult:
        """Cancel every order and close every position at market. The nuclear option."""
        self.stats.kills += 1
        errors: list[str] = []
        try:
            await self.rest.cancel_all()
        except DeltaApiError as exc:
            errors.append(f"cancel_all: {exc}")
        try:
            await self.rest.close_all_positions()
        except DeltaApiError as exc:
            errors.append(f"close_all: {exc}")
        await self._log(
            "kill",
            {"errors": errors},
            "LIVE KILL executed" + (f" with errors: {errors}" if errors else ""),
        )
        return LiveResult(not errors, error="; ".join(errors))

    # ---- fill wait ---------------------------------------------------------------------------------
    async def _await_fill(
        self, order: dict[str, Any], client_order_id: str, contract_value: float
    ) -> LiveResult:
        order_id = _i(order.get("id")) or None
        snap = order
        deadline = time.time() + self.FILL_TIMEOUT_S
        while True:
            if order_is_filled(snap):
                return LiveResult(
                    True,
                    fill=self._fill_from(snap, contract_value),
                    venue_order=snap,
                    order_id=order_id,
                )
            if order_is_dead(snap):
                return LiveResult(
                    False,
                    error=f"order {snap.get('state')}: {snap.get('cancellation_reason')}",
                    venue_order=snap,
                    order_id=order_id,
                )
            if time.time() >= deadline:
                return LiveResult(
                    False,
                    error=f"no fill within {self.FILL_TIMEOUT_S}s (state {snap.get('state')})",
                    venue_order=snap,
                    order_id=order_id,
                )
            await asyncio.sleep(self.FILL_POLL_S)
            ws_snap = self._order_updates.get(client_order_id)
            if ws_snap and order_is_filled(ws_snap):
                snap = {**snap, **ws_snap}
                continue
            try:
                snap = (
                    await self.rest.get_order(order_id)
                    if order_id
                    else await self.rest.get_order_by_client_id(client_order_id)
                )
            except DeltaApiError as exc:
                self.stats.last_error = str(exc)
                log.warning("live_fill_poll_failed", error=str(exc))

    def _fill_from(self, o: dict[str, Any], contract_value: float) -> Fill:
        price = _f(o.get("average_fill_price"))
        filled = _i(o.get("size")) - _i(o.get("unfilled_size"))
        fee = (
            _f(o.get("paid_commission"))
            or _f(o.get("commission"))
            or price * filled * contract_value * self.taker_fee_rate
        )
        return Fill(
            price=price,
            contracts=filled,
            ts=time.time(),
            fee=fee,
            slippage_bps=None,
            book_age_ms=None,
            levels=0,
        )

    def status(self) -> dict[str, Any]:
        return {
            "leverage": self.leverage,
            "orders_placed": self.stats.orders_placed,
            "orders_filled": self.stats.orders_filled,
            "orders_failed": self.stats.orders_failed,
            "cancels": self.stats.cancels,
            "kills": self.stats.kills,
            "leverage_set": self.stats.leverage_set,
            "last_error": self.stats.last_error,
            "last_order_ts": self.stats.last_order_ts,
        }
