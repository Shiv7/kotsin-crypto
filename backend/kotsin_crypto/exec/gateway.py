"""Order gateway — every order intent passes through here, in every mode (R9).

SHADOW records the decision and places nothing; PAPER fills against the live L2 via ``paper.py``;
LIVE_CAPPED and LIVE place real orders through ``LiveExecutor`` (``submit_live``). Enforced here: halt
(entries only — exits are always allowed), idempotency on ``client_order_id``, the LIVE_CAPPED caps
(symbol whitelist, contracts per order, concurrent positions, orders per day, daily notional, daily
loss, balance × leverage) and a consecutive-reject breaker.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..domain import Fill, Order, OrderIntent, Position, Purpose, new_id
from ..feed.book import Book
from .paper import NoBook, PaperMatcher

if TYPE_CHECKING:
    from .live import LiveExecutor


class Mode(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE_CAPPED = "LIVE_CAPPED"
    LIVE = "LIVE"


LIVE_MODES = (Mode.LIVE_CAPPED, Mode.LIVE)


class Decision(StrEnum):
    SUBMITTED = "SUBMITTED"
    PAPER_FILLED = "PAPER_FILLED"
    SHADOW_OK = "SHADOW_OK"
    SHADOW_WOULD_REJECT = "SHADOW_WOULD_REJECT"
    REJECTED_HALT = "REJECTED_HALT"
    REJECTED_CAP = "REJECTED_CAP"
    REJECTED_RISK = "REJECTED_RISK"
    DUP_BLOCKED = "DUP_BLOCKED"
    REJECTED_VENUE = "REJECTED_VENUE"


@dataclass(frozen=True, slots=True)
class LiveCaps:
    """Hard limits for LIVE_CAPPED. Defaults are sized for a ~$25 plumbing-test account."""

    symbols: tuple[str, ...] = ("BTCUSD", "ETHUSD")
    max_contracts: int = 1
    max_positions: int = 2
    max_orders_per_day: int = 6
    daily_notional_usd: float = 500.0
    daily_loss_usd: float = 3.0
    leverage: float = 5.0
    breaker_consecutive_rejects: int = 3


Caps = LiveCaps  # backward-compatible name


@dataclass(slots=True)
class LiveContext:
    """What the caps need to know at submit time; supplied by the engine."""

    balance: float
    open_positions: int
    day_pnl_usd: float
    price: float


@dataclass(slots=True)
class OrderResult:
    decision: Decision
    order: Order
    fill: Fill | None = None

    @property
    def filled(self) -> bool:
        return self.fill is not None


def live_capped_contracts(
    risk_contracts: int,
    caps: LiveCaps,
    *,
    balance: float,
    price: float,
    contract_value: float,
) -> tuple[int, str]:
    """LIVE_CAPPED sizing: ``min(cap, max(1, risk-based))`` when one contract's notional fits inside
    ``balance × leverage``; otherwise 0 with the reason. A tiny account trades one contract or nothing."""
    if price <= 0 or contract_value <= 0 or balance <= 0:
        return 0, "no price/balance"
    contracts = min(caps.max_contracts, max(1, int(risk_contracts)))
    notional = contracts * contract_value * price
    allowed = balance * caps.leverage
    if notional > allowed + 1e-9:
        return 0, f"notional ${notional:.2f} exceeds balance ${balance:.2f} × {caps.leverage:g}×"
    return contracts, "ok"


class Gateway:
    def __init__(
        self,
        *,
        books: Mapping[str, Book],
        matcher: PaperMatcher,
        mode: Callable[[], Mode],
        halted: Callable[[], tuple[bool, str]],
        caps: LiveCaps | None = None,
        live: LiveExecutor | None = None,
    ) -> None:
        self._books = books
        self._matcher = matcher
        self._mode = mode
        self._halted = halted
        self.caps = caps or LiveCaps()
        self.live = live
        self._seen: set[str] = set()
        self.orders_today = 0
        self.notional_today = 0.0
        self.consecutive_rejects = 0
        self.breaker_tripped = False
        self._day = self._today()

    @staticmethod
    def _today() -> str:
        return datetime.now(tz=UTC).strftime("%Y-%m-%d")

    def _rollover(self) -> None:
        d = self._today()
        if d != self._day:
            self._day, self.orders_today, self.notional_today = d, 0, 0.0

    def remember(self, client_order_id: str) -> None:
        """Seed idempotency from the ledger on boot."""
        self._seen.add(client_order_id)

    def _order_for(self, intent: OrderIntent, mode: Mode) -> Order:
        return Order(
            id=new_id("ord"),
            client_order_id=intent.client_order_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            side=intent.side,
            purpose=intent.purpose,
            contracts=intent.contracts,
            mode=mode.value,
            status="REJECTED",
            signal_id=intent.signal_id,
            position_id=intent.position_id,
            reason=intent.reason,
            ts=time.time(),
        )

    def _precheck(self, intent: OrderIntent, order: Order) -> OrderResult | None:
        if intent.client_order_id in self._seen:
            return self._reject(order, Decision.DUP_BLOCKED, "duplicate client_order_id")
        self._seen.add(intent.client_order_id)
        halted, why = self._halted()
        if halted and intent.purpose is Purpose.ENTRY:
            return self._reject(order, Decision.REJECTED_HALT, why or "halted")
        return None

    # ---- SHADOW / PAPER (synchronous) ------------------------------------------------------------
    def submit(
        self, intent: OrderIntent, *, contract_value: float, now_us: int | None = None
    ) -> OrderResult:
        self._rollover()
        mode = self._mode()
        order = self._order_for(intent, mode)
        pre = self._precheck(intent, order)
        if pre is not None:
            return pre

        if mode is Mode.SHADOW:
            order.status = "SHADOW"
            self.consecutive_rejects = 0
            return OrderResult(Decision.SHADOW_OK, order)

        if mode is Mode.PAPER:
            try:
                fill = self._matcher.fill(
                    intent,
                    self._books.get(intent.symbol),
                    contract_value=contract_value,
                    now_us=now_us,
                )
            except NoBook as exc:
                return self._reject(order, Decision.REJECTED_VENUE, str(exc))
            order.status, order.avg_price, order.filled = "FILLED", fill.price, fill.contracts
            order.fee, order.slippage_bps = fill.fee, fill.slippage_bps
            self.consecutive_rejects = 0
            self.orders_today += 1
            self.notional_today += fill.price * fill.contracts * contract_value
            return OrderResult(Decision.PAPER_FILLED, order, fill)

        return self._reject(
            order, Decision.REJECTED_VENUE, f"{mode.value} orders must go through submit_live()"
        )

    # ---- LIVE_CAPPED / LIVE (asynchronous) ---------------------------------------------------------
    def check_live_caps(
        self, intent: OrderIntent, ctx: LiveContext, *, contract_value: float
    ) -> str | None:
        """Reason the intent breaks a LIVE_CAPPED cap, or None. Exits are never capped."""
        if intent.purpose is not Purpose.ENTRY:
            return None
        c = self.caps
        if intent.symbol not in c.symbols:
            return f"{intent.symbol} not in live whitelist {list(c.symbols)}"
        if intent.contracts > c.max_contracts:
            return f"{intent.contracts} contracts > cap {c.max_contracts}"
        if ctx.open_positions >= c.max_positions:
            return f"{ctx.open_positions} positions open ≥ cap {c.max_positions}"
        if self.orders_today >= c.max_orders_per_day:
            return f"{self.orders_today} orders today ≥ cap {c.max_orders_per_day}"
        notional = intent.contracts * contract_value * ctx.price
        if self.notional_today + notional > c.daily_notional_usd:
            return f"daily notional ${self.notional_today + notional:.2f} > cap ${c.daily_notional_usd:.2f}"
        if ctx.day_pnl_usd <= -c.daily_loss_usd:
            return f"day P&L ${ctx.day_pnl_usd:.2f} ≤ −${c.daily_loss_usd:.2f} daily loss cap"
        if notional > ctx.balance * c.leverage + 1e-9:
            return f"notional ${notional:.2f} exceeds balance ${ctx.balance:.2f} × {c.leverage:g}×"
        return None

    async def submit_live(
        self,
        intent: OrderIntent,
        *,
        contract_value: float,
        ctx: LiveContext,
        stop_price: float | None = None,
        position: Position | None = None,
    ) -> OrderResult:
        self._rollover()
        mode = self._mode()
        order = self._order_for(intent, mode)
        if mode not in LIVE_MODES:
            return self._reject(
                order, Decision.REJECTED_VENUE, f"not in a live mode ({mode.value})"
            )
        if self.live is None:
            return self._reject(
                order, Decision.REJECTED_VENUE, "live executor not configured (no API keys)"
            )
        pre = self._precheck(intent, order)
        if pre is not None:
            return pre
        if mode is Mode.LIVE_CAPPED:
            why = self.check_live_caps(intent, ctx, contract_value=contract_value)
            if why:
                return self._reject(order, Decision.REJECTED_CAP, why)
        if intent.purpose is Purpose.ENTRY:
            if stop_price is None:
                return self._reject(order, Decision.REJECTED_RISK, "live entry needs a stop price")
            res = await self.live.place_entry(intent, stop_price)
        else:
            if position is None:
                return self._reject(order, Decision.REJECTED_RISK, "live exit needs the position")
            res = await self.live.place_exit(position, intent)
        if not res.ok or res.fill is None:
            return self._reject(order, Decision.REJECTED_VENUE, res.error or "no fill")
        fill = res.fill
        order.status, order.avg_price, order.filled, order.fee = (
            "FILLED",
            fill.price,
            fill.contracts,
            fill.fee,
        )
        order.note = f"venue order {res.order_id}"
        self.consecutive_rejects = 0
        self.orders_today += 1
        self.notional_today += fill.price * fill.contracts * contract_value
        return OrderResult(Decision.SUBMITTED, order, fill)

    def _reject(self, order: Order, decision: Decision, note: str) -> OrderResult:
        order.status, order.note = "REJECTED", note
        if decision is not Decision.DUP_BLOCKED:
            self.consecutive_rejects += 1
            if self.consecutive_rejects >= self.caps.breaker_consecutive_rejects:
                self.breaker_tripped = True
        return OrderResult(decision, order)

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self._mode().value,
            "orders_today": self.orders_today,
            "notional_today": round(self.notional_today, 2),
            "consecutive_rejects": self.consecutive_rejects,
            "breaker_tripped": self.breaker_tripped,
            "seen_ids": len(self._seen),
            "live_configured": self.live is not None,
            "caps": {
                "symbols": list(self.caps.symbols),
                "max_contracts": self.caps.max_contracts,
                "max_positions": self.caps.max_positions,
                "max_orders_per_day": self.caps.max_orders_per_day,
                "daily_notional_usd": self.caps.daily_notional_usd,
                "daily_loss_usd": self.caps.daily_loss_usd,
                "leverage": self.caps.leverage,
            },
        }


__all__ = [
    "LIVE_MODES",
    "Caps",
    "Decision",
    "Gateway",
    "LiveCaps",
    "LiveContext",
    "Mode",
    "OrderResult",
    "live_capped_contracts",
]
