# Research notes — what was read, what was taken, what was not

## Papers (read 2026-09-20)

### TradingAgents: Multi-Agents LLM Financial Trading Framework (arXiv 2412.20138 v7)
Role-structured LLM committee: analysts (fundamentals / sentiment / news / technical) → bull vs bear
researchers → trader → aggressive / neutral / conservative risk debate → portfolio manager.
Evaluation: AAPL, GOOGL, AMZN, **1 Jan – 29 Mar 2024** (three months, one of the strongest tech
quarters on record), baselines are MACD / KDJ+RSI / ZMR / SMA / buy-and-hold. Claims 23–26 % cumulative
return and Sharpe > 2 — the paper itself notes the Sharpe "exceeds our expected empirical range" because
"there were few pullbacks" in the window. No transaction costs, no slippage, daily decisions.

**Taken:** the role decomposition and the adversarial debate as a *reasoning scaffold*; structured
pydantic outputs with field descriptions as instructions; the `REVIEW` sentinel instead of a silent
Hold; the decision log with deferred outcome reflection; point-in-time discipline.
**Not taken:** the return claims (three months, three tickers, no costs), the daily/weekly cadence as a
signal source (an LLM cannot be a 5-minute signal engine), yfinance/news/social inputs (no keys, and
look-ahead was their biggest correction history).

### Trading-R1: Financial Trading with LLM Reasoning via Reinforcement Learning (arXiv 2509.11420)
Fine-tunes Qwen3-4B with a three-stage SFT→RFT curriculum (thesis STRUCTURE → evidence-grounded
CLAIMS → DECISION), GRPO, on a 100k-sample corpus (14 equities, 18 months, five data sources).
Evaluation on NVDA/AAPL/MSFT/AMZN/META/SPY, weekly horizon: cumulative returns of a few percent,
Sharpe around 1, versus GPT-4.1 at 3.2 % / 0.85 SR on NVDA. Its own limitations section names the
"thesis-to-decision gap" and data cost.

**Taken (the genuinely useful part):** §3.5 *volatility-driven discretisation* — forward returns over
several horizons, each divided by rolling realised volatility (Sharpe-like), combined 0.3/0.5/0.2,
cut at asymmetric percentiles (85/53/15/3) into five classes. Implemented in `committee/labels.py`
with 1h/4h/24h horizons on 5m bars (`label_series`) and as the grader of committee decisions
(`z_label` + Trading-R1's ordinal partial-credit reward, `ordinal_score`). Also its central design
claim: input quality beats longer chains of thought — hence the numeric evidence pack with citable keys.
**Not taken:** training a 4B model (needs a labelled corpus and GPUs; the paper's edge over GPT-4.1 is
marginal); equity-specific asymmetric drift assumptions (perps have none — cut-offs kept symmetric for
grading); weekly horizons.

## What this repo does with it (`backend/kotsin_crypto/committee/`)
- 8 structured Claude calls per symbol per run (3 analysts ∥ → debate → 3 risk stances ∥ → PM).
- Advisory only: can scale an entry 0.5–1.0× when `KC_COMMITTEE_SIZE_INFLUENCE=true`; never blocks,
  never enlarges, never originates. Off until `KC_ANTHROPIC_API_KEY` is set.
- Every decision is graded after its horizon on the vol-adjusted return and reflected on; resolved
  lessons are injected into later prompts (point-in-time).
- Cost guard: `KC_COMMITTEE_MAX_RUNS_PER_DAY`; estimated spend shown on the Committee page.

## Verdict
There is no "alpha" to lift from either paper. What is worth having — and is now in the code — is the
scaffolding: structured, evidence-cited reasoning; a self-grading decision log; and a principled,
volatility-adjusted label that can also train or evaluate any future signal-quality model on our own
tape. Whether the committee adds value here is an empirical question the graded log will answer.
