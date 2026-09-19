# Learnings — rules this codebase enforces

Distilled from operating a multi-strategy paper-trading stack on Indian markets for a year. Each rule
names the failure it prevents and *how this repo enforces it* — a rule that lives only in a document is
not a rule.

| # | Rule | Failure it prevents | Enforced by |
|---|---|---|---|
| R1 | Config is typed and closed | a key nothing reads, masked by a default that happens to match | `config.py`: `extra="forbid"` for `.env`, `assert_no_unknown_env()` for the process env; a test asserts every declared field is read somewhere |
| R2 | Comments cite code, or don't exist | doc drift ("SuperTrend(10,3)" while the code runs 7) | parameters live in one registry per strategy (`docs/strategies/<KEY>.md` §Parameters) generated from the strategy's config class |
| R3 | No shadow score without a promotion test and a sunset date | scores computed on the hot path, stamped into payloads, never used | every `Signal.evidence` key must be referenced by a gate, a sizer, or an open promotion ticket |
| R4 | No sentinel caps | `top_n=999` "selection" that selects nothing | caps are `int | None`; `None` means off and is logged as OFF at boot |
| R5 | Every gate declares what missing data means | one strategy fails open and its sibling fails closed on the same input, and neither alerts | `strategy/gates.py`: `Gate(on_missing=FAIL_OPEN \| FAIL_CLOSED)`; missing inputs are counted and alerted |
| R6 | Delete the consumer with the producer | wallets funded for strategies that can never fire | `StrategyKey` enum is the single registry; removing a key breaks every reference at import time |
| R7 | Record every candidate, not only the winners | "what did the filter reject, and would it have won?" is unanswerable | the signals table stores rejections with the failing gate; the UI shows them |
| R8 | Strategy keys are an enum, never a string | `"HOTSTOCKS".equals(...)` silently excluding `HOTSTOCKS_MOMENTUM` | `strategy/keys.py`; `import-linter` keeps it the only source |
| R9 | Mode is state, not an env var | a restart without `X_LIVE=true` silently reverting to paper for weeks | control table row + boot banner + Telegram echo; LIVE requires `armed_until` |
| R10 | The exchange is the source of truth for positions | open positions lost on restart | `exec/reconcile.py` on boot and every 30 s; mismatch freezes entries |
| R11 | One owner per rule | two services holding different time-stops for the same book | `risk/exits.py` is the only module that decides an exit |
| R12 | Enum branches must be reachable | `"DRYING_UP"` tested, `"QUIET"` emitted | producers and consumers share one enum; ruff/mypy + tests |
| R13 | No calibration on n < 30 | thresholds tuned to preserve two remembered trades | strategy docs carry the sample size; PR template asks for it |
| R14 | No live strategy without a backtest artefact | thresholds with no citation anywhere | `docs/strategies/<KEY>.md` §Artefact is required by the PR template |
| R15 | Exposure is aggregated by underlying | one trigger opening four funded positions | `risk/exposure.py` buckets BTC/ETH/SOL together |
| R16 | Config lands with the code that reads it | `retest.v2.*` keys with no reader on any branch | same as R1 |
| R17 | A placeholder constant is a bug with a due date | `avg20d=0.0 (TODO)` making a regime detector return UNKNOWN forever | `TODO(` comments must carry an issue link; CI greps for bare TODOs |

## Economics

- On NSE cash at ₹33k/position, round-trip cost was **0.299%**, 81% of it flat ₹40/order brokerage;
  break-even needed ~₹1.3 lakh per position and *entries, not exits, were the binding problem*.
- On Delta perps the same size costs **0.10%** taker / **0.04%** maker round trip plus one tick of
  spread. Fees are proportional, so size is a risk choice, not a cost constraint.
- Put fees, funding and slippage into the backtester before the first strategy. A null strategy must
  backtest to exactly −fees −funding (step 4 done-criterion).

## Operations

- Five JVMs with heaps sized for a 30 GB box relaunched by a watchdog onto a 2 GB box, plus a health
  probe that treated *slow* as *dead* and restarted the database every 3 minutes. Hence: one process,
  memory limits in the unit file, staggered start, and no restart on a single failed probe.
- `pkill -f name` kills the shell that runs it; kill by PID, wait on an artefact.
- Never reimplement maths the engine already has for a replay — replays that did so erred −80% … +185%.
  Here the backtester imports the live code.
- Trade statistics cluster by session/day; always run a within-day permutation test before believing a
  split, and say plainly when n is too small.
- "Has this topic ever carried a message?" was the cheapest liveness test and caught a strategy that
  had never fired in its lifetime. Every strategy exposes `signals_emitted_total`.
