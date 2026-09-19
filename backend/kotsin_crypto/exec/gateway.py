"""Order gateway — every order intent passes through here, in every mode (R9).

SHADOW records the decision and places nothing; PAPER fills against the live L2 via ``paper.py``;
LIVE_CAPPED / LIVE will place real orders under caps (step 8 — refused until then, loudly).
Enforced here: halt (entries only — exits are always allowed), idempotency on ``client_order_id``,
caps, and a consecutive-reject breaker.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from ..domain import Fill, Order, OrderIntent, Purpose, new_id
from ..feed.book import Book
from .paper import NoBook, PaperMatcher


class Mode(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE_CAPPED = "LIVE_CAPPED"
    LIVE = "LIVE"


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
class Caps:
    max_contracts: int = 1
    daily_notional_usd: Decimal = Decimal(500)
    max_orders_per_day: int = 3
    breaker_consecutive_rejects: int = 3


@dataclass(slots=True)
class OrderResult:
    decision: Decision
    order: Order
    fill: Fill | None = None

    @property
    def filled(self) -> bool:
        return self.fill is not None


class Gateway:
    def __init__(
        self,
        *,
        books: Mapping[str, Book],
        matcher: PaperMatcher,
        mode: Callable[[], Mode],
        halted: Callable[[], tuple[bool, str]],
        caps: Caps | None = None,
    ) -> None:
        self._books = books
        self._matcher = matcher
        self._mode = mode
        self._halted = halted
        self.caps = caps or Caps()
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

    def submit(
        self, intent: OrderIntent, *, contract_value: float, now_us: int | None = None
    ) -> OrderResult:
        self._rollover()
        mode = self._mode()
        order = Order(
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

        if intent.client_order_id in self._seen:
            return self._reject(order, Decision.DUP_BLOCKED, "duplicate client_order_id")
        self._seen.add(intent.client_order_id)

        halted, why = self._halted()
        if halted and intent.purpose is Purpose.ENTRY:
            return self._reject(order, Decision.REJECTED_HALT, why or "halted")

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
            order, Decision.REJECTED_VENUE, f"{mode.value} orders arrive with exec/live.py (step 8)"
        )

    def _reject(self, order: Order, decision: Decision, note: str) -> OrderResult:
        order.status, order.note = "REJECTED", note
        if decision is not Decision.DUP_BLOCKED:
            self.consecutive_rejects += 1
            if self.consecutive_rejects >= self.caps.breaker_consecutive_rejects:
                self.breaker_tripped = True
        return OrderResult(decision, order)

    def stats(self) -> dict[str, object]:
        return {
            "mode": self._mode().value,
            "orders_today": self.orders_today,
            "notional_today": round(self.notional_today, 2),
            "consecutive_rejects": self.consecutive_rejects,
            "breaker_tripped": self.breaker_tripped,
            "seen_ids": len(self._seen),
        }
