---
strategy_key:        # exact StrategyKey enum member, e.g. CAN2
display_name:
symbols:             # BTCUSD, ETHUSD, SOLUSD
timeframes:          # e.g. 5m (trigger), 1m (management)
status:              # DESIGNED | BACKTESTED | PAPER | LIVE_CAPPED | LIVE | RETIRED
since:               # absolute date
doc_verified_on:     # date this doc was checked against the code
---

# <STRATEGY>

> Every behavioural claim cites `backend/kotsin_crypto/strategy/<file>.py:LINE`. Unverified → `[UNVERIFIED]`.
> DESIGNED vs LIVE are separated; where they differ, LIVE wins and the gap is listed in §8.

## 1. Thesis
Two or three sentences: what edge, why it should persist on a 24/7 perpetual market.
**Falsifier:** the one observation that proves the edge is gone.

## 2. Inputs
| UnifiedBar field | Used for | on_missing |
|---|---|---|

## 3. Parameters (one source of truth: the strategy's config class)
| Name | Default | Sweep range | Fitted on (period, n) | Last changed |
|---|---|---|---|---|

## 4. Entry
| # | Condition | Threshold | Gate `on_missing` | Hard gate or score? |
|---|---|---|---|---|
Combination rule: all-of / n-of-m / score ≥ T.

## 5. Exit (owned by risk/exits.py — this section documents the *requested* profile)
Initial stop · ratchet ladder · time-stop · funding-aware flatten.

## 6. Sizing
Risk per trade · leverage cap · liquidation-distance guard.

## 7. Artefacts
| Stage | Period | Trades | Net edge / trade | Permutation p | File |
|---|---|---|---|---|---|
| Backtest (OOS) | | | | | `research/out/<KEY>-<date>.md` |
| Paper | | | | | |
| Live capped | | | | | |

## 8. DESIGNED vs LIVE gaps
## 9. Known traps
