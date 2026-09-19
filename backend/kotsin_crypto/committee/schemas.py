"""Structured outputs for the committee. Field descriptions are the model's instructions (the
TradingAgents pattern); every claim must cite evidence keys from the pack, and a decision that cannot
be grounded is a REVIEW, never a silent HOLD (their #1170 lesson)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Rating(StrEnum):
    STRONG_SELL = "STRONG_SELL"
    SELL = "SELL"
    HOLD = "HOLD"
    BUY = "BUY"
    STRONG_BUY = "STRONG_BUY"


RATING_RANK: dict[Rating, int] = {r: i for i, r in enumerate(Rating)}


def agreement_multiplier(rating: Rating, side: str, floor: float = 0.5) -> float:
    """How much of a signal's size the committee stance allows: agreement 1.0, hold 0.6, disagreement
    the floor. The committee can shrink an entry, never block it and never enlarge it."""
    rank = RATING_RANK[rating] - 2  # -2..+2
    signed = rank if side.upper() in ("LONG", "BUY") else -rank
    table = {2: 1.0, 1: 0.9, 0: 0.6, -1: floor, -2: floor}
    return max(floor, table[signed])


class AnalystReport(BaseModel):
    role: Literal["market", "flow", "derivatives"] = Field(
        description="Which analyst produced this report."
    )
    summary: str = Field(
        description="Two or three sentences on what the evidence for this role says right now."
    )
    bullish: list[str] = Field(
        description="Bullish observations, each ending with the evidence keys it rests on in square brackets, e.g. [px.ret_4h, micro.ofi_5m]."
    )
    bearish: list[str] = Field(
        description="Bearish observations, each citing evidence keys in square brackets."
    )
    evidence_keys: list[str] = Field(
        description="Every evidence key referenced above, exactly as written in the pack."
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="0 = the evidence for this role is missing or contradictory; 1 = unambiguous.",
    )


class Debate(BaseModel):
    bull_case: str = Field(
        description="The strongest case for being LONG over the horizon, built only from the analysts' cited evidence."
    )
    bear_case: str = Field(
        description="The strongest case for being SHORT (or flat) over the horizon, built only from cited evidence."
    )
    bull_strongest_point: str = Field(
        description="The single bull point the bear could not rebut, with its evidence keys."
    )
    bear_strongest_point: str = Field(
        description="The single bear point the bull could not rebut, with its evidence keys."
    )
    unresolved: list[str] = Field(
        description="Questions the evidence pack cannot answer; these are reasons for lower conviction, not for HOLD by default."
    )


class RiskReview(BaseModel):
    stance: Literal["aggressive", "neutral", "conservative"]
    concerns: list[str] = Field(
        description="Concrete risks to the proposed stance, each citing evidence keys (funding, liquidation distance, toxicity, event risk, thin book)."
    )
    size_adjustment: float = Field(
        ge=0,
        le=1,
        description="Fraction of a standard position this reviewer would allow (1 = full).",
    )
    invalidation: str = Field(
        description="What observation would prove the stance wrong, as a price level or a measurable condition."
    )


class Decision(BaseModel):
    rating: Rating = Field(
        description="Committee stance for the horizon. Conflicting arguments alone are not HOLD: commit to the stronger side sized by how decisively it wins; HOLD only when evidence is balanced after weighing."
    )
    conviction: float = Field(ge=0, le=1, description="How decisively the stronger side won.")
    size_multiplier: float = Field(
        ge=0,
        le=1,
        description="Fraction of a standard position appropriate now, after the risk reviews.",
    )
    horizon_h: int = Field(ge=1, le=72, description="Hours the stance is expected to hold.")
    thesis_market: str = Field(description="Price structure, volatility and levels, citing keys.")
    thesis_flow: str = Field(description="Order flow, toxicity and book conditions, citing keys.")
    thesis_derivatives: str = Field(
        description="Funding, basis, open interest and options positioning, citing keys."
    )
    thesis_risk: str = Field(description="What the risk reviews changed and why.")
    evidence_keys: list[str] = Field(description="Every evidence key the thesis rests on.")
    invalidation: str = Field(description="The observation that would flip this decision.")
    review: bool = Field(
        description="True when the pack is too thin or contradictory to support any rating; the engine then ignores this run."
    )


class Reflection(BaseModel):
    lesson: str = Field(
        description="Two to four plain sentences: what the realised, volatility-adjusted outcome says about the call, which part of the thesis it supports or undercuts, and one concrete lesson for the next similar analysis."
    )
    thesis_supported: bool = Field(
        description="Whether the outcome window supported the thesis direction."
    )
