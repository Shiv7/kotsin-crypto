"""The committee, TradingAgents-shaped and cost-bounded: three analysts in parallel → one adversarial
bull/bear call → three risk stances in parallel → one portfolio-manager decision. Eight structured
calls per run. Every role sees the same evidence pack and must cite its keys."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .llm import LLM
from .schemas import AnalystReport, Debate, Decision, Reflection, RiskReview

SYSTEM_CORE = """You are one role on a trading committee for a personal account trading USD-settled perpetual futures on Delta Exchange India (BTCUSD, ETHUSD, SOLUSD). The account runs at low leverage (≤3×), pays 0.05% taker fees each way and funding every 8 hours, and holds positions for hours, not weeks.

Rules that apply to every role:
- Reason ONLY from the evidence pack you are given. It is point-in-time and complete; you have no news, no social feeds and no memory of prices beyond it. If something you would need is missing, say so — never invent it.
- Cite the evidence keys you rely on, exactly as written (e.g. deriv.funding_annualized_pct). A claim without a key is worth nothing.
- Quantify: prefer "funding +11%/yr, basis −2.4 bps, buy ratio 0.31 over 15m" to adjectives.
- A perpetual has no upward drift: HOLD is a real answer, but conflict between roles alone is not a reason for it.
- Keep every text field short; the reader is another role, not a client.
"""

ROLE_PROMPTS = {
    "market": "You are the MARKET analyst: price structure, ranges, momentum over 1h/4h/24h, realised volatility, position in the 24h range, and what the 5-minute bars show about the most recent hour.",
    "flow": "You are the FLOW analyst: order flow imbalance (L1 and L5), taker buy ratios over 5m/15m, Kyle's lambda (price impact), VPIN (flow toxicity), spread and depth, trade intensity and large-trade share. Say what the tape is doing and whether it confirms or fights the price move.",
    "derivatives": "You are the DERIVATIVES analyst: funding rate and its annualised cost, basis vs spot, open interest and its 6h change, the nearest options expiry (ATM IV, put/call OI, max pain) and what they imply for crowding, squeeze risk and carry over the next hours.",
}

RISK_PROMPTS = {
    "aggressive": "You are the AGGRESSIVE risk reviewer: you argue for taking the opportunity at full size when the evidence supports it, but you must still name the concrete risks and an invalidation level.",
    "neutral": "You are the NEUTRAL risk reviewer: you weigh reward against the measurable risks (funding cost over the horizon, liquidation distance at 3× leverage, toxicity, thin book) and propose a size that balances them.",
    "conservative": "You are the CONSERVATIVE risk reviewer: you protect capital first; list every way this stance loses money, and propose the size (possibly zero) at which the account is comfortable being wrong.",
}


@dataclass(slots=True)
class CommitteeRun:
    symbol: str
    started_ts: float
    analysts: list[AnalystReport] = field(default_factory=list)
    debate: Debate | None = None
    risk: list[RiskReview] = field(default_factory=list)
    decision: Decision | None = None
    calls: int = 0
    seconds: float = 0.0
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "started_ts": self.started_ts,
            "analysts": [a.model_dump() for a in self.analysts],
            "debate": self.debate.model_dump() if self.debate else None,
            "risk": [r.model_dump() for r in self.risk],
            "decision": self.decision.model_dump() if self.decision else None,
            "calls": self.calls,
            "seconds": round(self.seconds, 1),
            "error": self.error,
        }


def _render_reports(reports: list[AnalystReport]) -> str:
    out = []
    for r in reports:
        out.append(f"[{r.role} analyst, confidence {r.confidence:.2f}] {r.summary}")
        out.extend(f"  + {b}" for b in r.bullish)
        out.extend(f"  - {b}" for b in r.bearish)
    return "\n".join(out)


async def run_committee(
    llm: LLM,
    *,
    symbol: str,
    evidence_text: str,
    past_context: str = "",
    portfolio_text: str = "",
    horizon_h: int = 4,
) -> CommitteeRun:
    run = CommitteeRun(symbol=symbol, started_ts=time.time())
    common = f"Horizon: the next {horizon_h} hours.\n\nEVIDENCE PACK\n{evidence_text}\n"
    if portfolio_text:
        common += f"\nCURRENT BOOK\n{portfolio_text}\n"
    try:
        run.analysts = list(
            await asyncio.gather(
                *[
                    llm.structured(
                        system=SYSTEM_CORE + ROLE_PROMPTS[role],
                        user=common + f"\nWrite the {role} analyst report. Set role to '{role}'.",
                        schema=AnalystReport,
                        effort="medium",
                    )
                    for role in ("market", "flow", "derivatives")
                ]
            )
        )
        run.calls += 3
        reports = _render_reports(run.analysts)
        run.debate = await llm.structured(
            system=SYSTEM_CORE
            + "You run the RESEARCH debate: first argue the bull case as its strongest advocate, then the bear case as its strongest advocate, then identify the point each side could not rebut. Use only the analysts' cited evidence.",
            user=common
            + f"\nANALYST REPORTS\n{reports}\n"
            + (f"\nPAST DECISIONS AND LESSONS\n{past_context}\n" if past_context else "")
            + "\nProduce the debate.",
            schema=Debate,
            effort="medium",
        )
        run.calls += 1
        debate_text = f"BULL: {run.debate.bull_case}\nBEAR: {run.debate.bear_case}\nUnrebutted bull point: {run.debate.bull_strongest_point}\nUnrebutted bear point: {run.debate.bear_strongest_point}\nUnresolved: {'; '.join(run.debate.unresolved)}"
        run.risk = list(
            await asyncio.gather(
                *[
                    llm.structured(
                        system=SYSTEM_CORE + RISK_PROMPTS[stance],
                        user=common
                        + f"\nANALYST REPORTS\n{reports}\n\nDEBATE\n{debate_text}\n\nWrite the {stance} risk review. Set stance to '{stance}'.",
                        schema=RiskReview,
                        effort="medium",
                    )
                    for stance in ("aggressive", "neutral", "conservative")
                ]
            )
        )
        run.calls += 3
        risk_text = "\n".join(
            f"[{r.stance}] size {r.size_adjustment:.2f}; invalidation: {r.invalidation}; concerns: {' | '.join(r.concerns)}"
            for r in run.risk
        )
        run.decision = await llm.structured(
            system=SYSTEM_CORE
            + "You are the PORTFOLIO MANAGER. Synthesise the analysts, the debate and the three risk reviews into one decision for the horizon. Commit to the stronger side sized by how decisively it wins; choose HOLD only when the evidence is balanced after weighing; set review=true only when the pack is too thin to support any rating. Weigh the risk reviewers on their merits, not their labels.",
            user=common
            + f"\nANALYST REPORTS\n{reports}\n\nDEBATE\n{debate_text}\n\nRISK REVIEWS\n{risk_text}\n"
            + (f"\nPAST DECISIONS AND LESSONS\n{past_context}\n" if past_context else "")
            + "\nDeliver the decision.",
            schema=Decision,
            effort="high",
            max_tokens=6000,
        )
        run.calls += 1
    except Exception as exc:
        run.error = f"{type(exc).__name__}: {exc}"
    run.seconds = time.time() - run.started_ts
    return run


async def reflect(
    llm: LLM, *, decision_text: str, realized_pct: float, z: float, truth: str, horizon_h: int
) -> Reflection:
    return await llm.structured(
        system="You are a trading analyst reviewing your own committee's past decision now that the outcome is known. Be specific and terse; your text is stored verbatim and re-read by future runs.",
        user=f"Outcome over {horizon_h}h: realised return {realized_pct:+.2f}%, volatility-adjusted z = {z:+.2f}, which grades as {truth}.\n\nThe decision was:\n{decision_text}\n\nWrite the reflection.",
        schema=Reflection,
        effort="low",
        max_tokens=1000,
    )
