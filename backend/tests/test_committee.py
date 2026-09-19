from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np

from kotsin_crypto.committee.labels import label_series, ordinal_score, vol_adjusted_z, z_label
from kotsin_crypto.committee.llm import FakeLLM
from kotsin_crypto.committee.memory import DecisionLog
from kotsin_crypto.committee.pipeline import run_committee
from kotsin_crypto.committee.schemas import (
    AnalystReport,
    Debate,
    Decision,
    Rating,
    RiskReview,
    agreement_multiplier,
)


def _fake() -> FakeLLM:
    return FakeLLM(
        {
            AnalystReport: lambda system, user: AnalystReport(
                role="market"
                if "market" in system.lower().split("you are the")[1][:20]
                else ("flow" if "flow analyst" in system.lower() else "derivatives"),
                summary="s",
                bullish=["up [px.ret_4h_pct]"],
                bearish=["funding [deriv.funding_annualized_pct]"],
                evidence_keys=["px.ret_4h_pct"],
                confidence=0.6,
            ),
            Debate: Debate(
                bull_case="b",
                bear_case="r",
                bull_strongest_point="p [px.ret_4h_pct]",
                bear_strongest_point="q [deriv.basis_bps]",
                unresolved=[],
            ),
            RiskReview: lambda system, user: RiskReview(
                stance="aggressive"
                if "AGGRESSIVE" in system
                else ("neutral" if "NEUTRAL" in system else "conservative"),
                concerns=["c [micro.vpin_fast]"],
                size_adjustment=0.7,
                invalidation="close below 80000",
            ),
            Decision: Decision(
                rating=Rating.BUY,
                conviction=0.65,
                size_multiplier=0.8,
                horizon_h=4,
                thesis_market="m",
                thesis_flow="f",
                thesis_derivatives="d",
                thesis_risk="r",
                evidence_keys=["px.ret_4h_pct"],
                invalidation="close below 80000",
                review=False,
            ),
        }
    )


def test_pipeline_makes_eight_calls_and_a_decision() -> None:
    llm = _fake()
    run = asyncio.run(
        run_committee(
            llm, symbol="BTCUSD", evidence_text="px.last: 1", past_context="", portfolio_text="flat"
        )
    )
    assert run.error is None and run.decision is not None and run.decision.rating is Rating.BUY
    assert run.calls == 8 and len(llm.calls) == 8
    assert [r.role for r in run.analysts] == ["market", "flow", "derivatives"]
    assert [r.stance for r in run.risk] == ["aggressive", "neutral", "conservative"]
    assert "EVIDENCE PACK" in llm.calls[0][2] and "RISK REVIEWS" in llm.calls[-1][2]
    j = run.to_json()
    assert j["decision"]["rating"] == "BUY" and j["calls"] == 8


def test_pipeline_error_is_captured_not_raised() -> None:
    llm = FakeLLM({})  # KeyError on first schema
    run = asyncio.run(run_committee(llm, symbol="BTCUSD", evidence_text="x"))
    assert run.decision is None and run.error and "KeyError" in run.error


def test_labels_and_scores() -> None:
    assert (
        z_label(1.5) is Rating.STRONG_BUY
        and z_label(0.5) is Rating.BUY
        and z_label(0.0) is Rating.HOLD
    )
    assert z_label(-0.5) is Rating.SELL and z_label(-2) is Rating.STRONG_SELL
    assert (
        ordinal_score(Rating.BUY, Rating.BUY) == 1.0
        and ordinal_score(Rating.STRONG_BUY, Rating.STRONG_SELL) == 0.0
    )
    assert ordinal_score(Rating.BUY, Rating.HOLD) == 0.75
    rng = np.random.default_rng(1)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 3000)))
    _z, labels = label_series(closes)
    lab = [x for x in labels if x is not None]
    assert len(lab) > 2000
    frac = {r: sum(1 for x in lab if x is r) / len(lab) for r in Rating}
    assert (
        abs(frac[Rating.STRONG_BUY] - 0.15) < 0.03
        and abs(frac[Rating.HOLD] - 0.38) < 0.04
        and abs(frac[Rating.STRONG_SELL] - 0.03) < 0.02
    )
    assert np.isnan(vol_adjusted_z(closes[:10])).all()
    short = vol_adjusted_z(closes[:40])  # shorter than the 288-bar horizon: must not raise
    assert len(short) == 40 and np.isfinite(short).sum() > 0


def test_agreement_multiplier_never_blocks_or_enlarges() -> None:
    assert agreement_multiplier(Rating.STRONG_BUY, "LONG") == 1.0
    assert agreement_multiplier(Rating.BUY, "LONG") == 0.9
    assert agreement_multiplier(Rating.HOLD, "SHORT") == 0.6
    assert agreement_multiplier(Rating.STRONG_BUY, "SHORT") == 0.5
    assert agreement_multiplier(Rating.SELL, "SHORT") == 0.9
    assert all(0.5 <= agreement_multiplier(r, s) <= 1.0 for r in Rating for s in ("LONG", "SHORT"))


def test_decision_log_roundtrip_and_context(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "d.json")
    log.append(
        {
            "id": "a",
            "symbol": "BTCUSD",
            "ts": 1_000.0,
            "rating": "BUY",
            "conviction": 0.7,
            "size_multiplier": 0.8,
            "horizon_h": 4,
            "thesis": {},
            "invalidation": "x",
            "pending": True,
            "mark_at_decision": 100.0,
        }
    )
    log.append(
        {
            "id": "b",
            "symbol": "ETHUSD",
            "ts": 2_000.0,
            "rating": "SELL",
            "conviction": 0.6,
            "size_multiplier": 0.7,
            "horizon_h": 4,
            "thesis": {},
            "invalidation": "y",
            "pending": True,
            "mark_at_decision": 50.0,
        }
    )
    assert len(log.pending()) == 2 and log.latest("BTCUSD")["id"] == "a"
    e = log.resolve(
        "a",
        mark_now=101.0,
        realized_pct=1.0,
        z=0.6,
        reflection="went up",
        thesis_supported=True,
        now=20_000.0,
    )
    assert e and e["truth"] == "BUY" and e["score"] == 1.0 and not e["pending"]
    log.resolve(
        "b",
        mark_now=52.0,
        realized_pct=4.0,
        z=1.3,
        reflection="squeezed",
        thesis_supported=False,
        now=20_000.0,
    )
    ctx = log.past_context("BTCUSD")
    assert "Past committee decisions on BTCUSD" in ctx and "went up" in ctx and "squeezed" in ctx
    reloaded = DecisionLog(tmp_path / "d.json")
    assert len(reloaded.entries) == 2 and reloaded.stats()["resolved"] == 2
    assert (
        reloaded.stats()["mean_score_by_rating"]["SELL"] == 0.25
    )  # SELL vs STRONG_BUY truth: 3 ranks apart
