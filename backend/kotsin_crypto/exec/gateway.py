"""Order gateway — every order intent passes through here, in every mode (LEARNINGS R9).

Modes: SHADOW (record the decision, place nothing), PAPER (fill against the live L2 via paper.py),
LIVE_CAPPED (real orders under hard caps), LIVE. The mode is a row in the control table; LIVE* must be
armed with an expiry. Caps, a consecutive-reject breaker, the halt flag and idempotency
(``client_order_id == signal_id``) are enforced here. Step 6 implements it; the vocabulary is fixed now.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Mode(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE_CAPPED = "LIVE_CAPPED"
    LIVE = "LIVE"


class Decision(StrEnum):
    SUBMITTED = "SUBMITTED"  # real order accepted by the venue
    PAPER_FILLED = "PAPER_FILLED"  # matched against the live book
    SHADOW_OK = "SHADOW_OK"  # would have been placed
    SHADOW_WOULD_REJECT = "SHADOW_WOULD_REJECT"  # a gate would have rejected it
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
