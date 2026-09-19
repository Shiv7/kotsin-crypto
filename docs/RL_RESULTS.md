# RL results — R0/R1 (walk-forward, out of sample)

Status: first run, 2026-09-19/20. Everything below is reproducible from the CLI commands listed;
artefacts live in `backend/data/rl/` (git-ignored) and are browsable on the **RL** page of the UI
(`/api/rl/runs`). **Bottom line: neither the learned exit policy nor the parameter bandit beats its
baseline out of sample; the entries (CAN2 test parameters) lose ~0.4 R per trade on this period, of
which ~0.38 R is fees.**

## What was built

| Piece | Module | Purpose |
|---|---|---|
| Archive replay | `research/replay.py` | rebuilds micro-aware 1m bars from the JSONL archive through the engine's own builders; deterministic |
| Feature frames | `research/features.py`, `research/data.py` | point-in-time 5m features + Trading-R1 labels, Parquet export |
| Evaluation | `research/eval.py` | expanding walk-forward folds, cost stress, day-blocked permutation tests |
| Exit environment | `research/env.py` | gym-style episode = one position; tighten-only actions; rewards in net R |
| Exit policies | `research/rl/policies.py` | hand ladder (baseline), behaviour policies, learned (FQI) |
| Offline RL | `research/rl/exit_policy.py` | episodes from real CAN2 signals → behaviour rollouts → Fitted Q-Iteration → OOS folds vs ladder |
| Bandit | `research/rl/bandit.py` | contextual Thompson sampling over 24 CAN2 parameter sets by regime |
| Backtester hook | `research/backtest.py` | `BacktestConfig(exit_policy=...)`; default path byte-identical |

## Commands

```bash
cd backend
uv run python -m kotsin_crypto.research.replay --archive data/archive --symbol BTCUSD --out data/rl/replay_BTCUSD.parquet
uv run python -m kotsin_crypto.research.data export --symbols BTCUSD,ETHUSD --start 2025-06-01 --end 2026-09-19 --out data/rl/features
uv run python -m kotsin_crypto.research.rl.exit_policy run --symbols BTCUSD,ETHUSD --start 2025-06-01 --end 2026-09-19 --folds 6
uv run python -m kotsin_crypto.research.rl.exit_policy dataset --symbols BTCUSD,ETHUSD --start 2025-06-01 --end 2026-09-19   # transitions .npz
uv run python -m kotsin_crypto.research.rl.bandit run --symbols BTCUSD,ETHUSD --start 2025-06-01 --end 2026-09-19 --folds 6
```

## Data

- 1m history: Delta India REST, cached per UTC day (`data/history/1m/<SYMBOL>/`), BTCUSD + ETHUSD, 2025-06-01 → 2026-09-19 (475 days each).
- Archive replay (real tape, 2026-09-19 20:08Z →, 53 MB of JSONL across all channels): 88 BTCUSD 1m bars
  (87 live + 1 partial), all 88 carrying microstructure — ~600 book updates/min, median spread 0.07 bps,
  median Kyle λ (15m) 0.39 bps per 1k, median VPIN-fast 0.46, median 57 trades/min. Wall-clock 1.7 s.
  Replaying the same archive twice gives byte-identical Parquet (synthetic archive in
  `test_rl_features_replay.py`, and verified on a frozen copy of the real tape). One subtlety: unless
  `--daily-volume` is given, the VPIN bucket size is derived from the archive's own volume, so replaying
  an archive that is still growing changes the `vpin`/`vpin_fast` columns (and only those) between runs
  — pass `--daily-volume` when comparing across archive versions.
- Feature frames: 136,800 5m rows per symbol (684,000 1m bars each), 21 MB Parquet per symbol, exported
  offline from the cache in ~18 s per symbol. The point-in-time property is tested by prefix equality
  (`test_features_are_point_in_time`).
- Offline RL dataset: `transitions_2025-06-01_2026-09-19.npz` — 10,463 episodes (BTCUSD 5,402 / ETHUSD
  5,061) × 3 behaviour policies = 192,294 transitions of 16 obs dims, 17.7 MB.

**Important:** the 1m history used for both experiments is Delta's REST candle backfill, which carries no
tape microstructure. The three microstructure observation dims (`buy_ratio`, `vpin_fast`, `kyle_bps`)
are therefore constant in these runs; only the replay path (real tape) fills them.

## Experiment 1 — exit policy (FQI) vs the hand R-ladder

Artefact `exit_policy_20260919_2129.json`. Setup: episodes are every CAN2 signal (test parameters,
`k_surge=2.5`) on 5m bars, BTCUSD + ETHUSD, 2025-06-01 → 2026-09-19; fill at the next bar's open ± 1 bp;
fees 5 bps per side; time stop 48 bars. Six expanding walk-forward folds; in each fold FQI (40 iterations,
γ = 0.99, ridge 1.0, quadratic features, actions with < 25 supporting transitions masked) is trained on
behaviour rollouts of the training episodes only, then the greedy policy and the hand ladder are run on
the *same* test episodes (paired). Wall-clock for the whole experiment: **53 s**. Seed 0.

**Headline: the learned policy does not beat the ladder.** Mean OOS net R per trade, mean of fold means:
policy **−0.447** vs ladder **−0.451** (episode-weighted pooled over the 7,866 OOS episodes: −0.452 vs
−0.455). The +0.004 R difference is not distinguishable from zero in any fold (paired day-blocked
permutation p = 0.29 – 0.93). Both policies lose money because the entries lose money.

| fold | train eps | test eps | policy mean R | ladder mean R | Δ | win % pol / lad | hold bars pol / lad | paired p |
|---|---|---|---|---|---|---|---|---|
| 0 | 2,597 | 1,229 | −0.362 | −0.381 | +0.019 | 13.3 / 25.5 | 11.4 / 10.1 | 0.73 |
| 1 | 3,826 | 1,315 | −0.562 | −0.573 | +0.011 | 20.6 / 23.3 | 5.3 / 11.0 | 0.68 |
| 2 | 5,141 | 1,235 | −0.229 | −0.271 | +0.042 | 17.9 / 27.0 | 12.2 / 11.5 | 0.34 |
| 3 | 6,376 | 1,370 | −0.493 | −0.497 | +0.004 | 19.2 / 26.1 | 3.1 / 12.1 | 0.93 |
| 4 | 7,746 | 1,349 | −0.456 | −0.460 | +0.004 | 22.5 / 25.1 | 3.6 / 11.3 | 0.93 |
| 5 | 9,095 | 1,368 | −0.581 | −0.525 | −0.056 | 16.7 / 25.4 | 3.3 / 13.1 | 0.29 |

What the policy actually learned (folds 3–5, where it has ≥ 6k training episodes): get out fast. It
tightens the stop to the current price within ~3 bars, so 63–65 % of its exits are `stop_gap` (next open
through the stop) versus 6–7 % for the ladder, and it accepts a lower win rate for a shorter, slightly
less negative hold. That is a rational response to entries with negative expectancy, not an exit edge:
under cost stress (fees × 1.5 and slippage × 2) both policies move together (mean of fold means −0.710
vs −0.714).

Fee drag is identical for both policies by construction and is large: 0.28 – 0.46 R per round trip per
fold (mean 0.38 R). With an average gross outcome near −0.07 R, no exit rule can make these trades
profitable; the lever is entry selection / stop width, not the exit ladder.

Final policy (trained on all 192,294 transitions; action support hold 121,488 · lock_0r 33,798 ·
lock_1r 13,372 · lock_2r 6,625 · lock_3r 3,745 · trail_1_5r 4,646 · trail_1r 2,865 · trail_0_5r 2,889 ·
exit_now 2,866) is stored in the artefact and loads through `BacktestConfig(exit_policy="data/rl/exit_policy_20260919_2129.json")`.
**It is not recommended for live use**; the ladder stays the default.

## Experiment 2 — contextual bandit over CAN2 parameters

Artefact `bandit_20260919_2148.json`. Setup: 24 arms = `k_surge ∈ {2, 2.5, 3, 4}` × `n_lookback ∈
{6, 12, 24}` × `sl_atr_mult ∈ {1.0, 1.5}`; each arm is a full breaker-free backtest (100k wallet, 0.1 %
risk, max 3 positions) per symbol — 48 backtests, **175,432 trades** in total, 8,154 for the default arm
`k2.5_n12_sl1.5`. Context = 12 regimes (48-bar realised-vol tercile with fixed cut-offs × UTC session).
Per fold, a Gaussian Thompson posterior per (regime, arm) is fitted on the training window's trades and
the arm with the highest posterior mean (≥ 10 trades, else the default) is fixed per regime for the test
window; the policy's test trades are compared with the default arm's test trades (unpaired, day-blocked
permutation). Wall-clock **1,217 s** (20.3 min), seed 0.

**Headline: no arm has positive expectancy in any regime, and the bandit's choice is not distinguishable
from the default.** Mean OOS net R per trade, mean of fold means: bandit **−0.378** vs default **−0.405**
(pooled over 4,370 bandit trades vs 6,137 default trades: −0.378 vs −0.408). Better in 3 of 6 folds;
p = 0.22 – 0.97.

| fold | n bandit | n default | bandit mean R | default mean R | Δ | win % bandit / default | regimes changed | p |
|---|---|---|---|---|---|---|---|---|
| 0 | 858 | 998 | −0.302 | −0.374 | +0.072 | 26.0 / 24.5 | 11 / 12 | 0.28 |
| 1 | 735 | 1,047 | −0.550 | −0.532 | −0.018 | 23.5 / 24.5 | 12 / 12 | 0.80 |
| 2 | 680 | 971 | −0.231 | −0.206 | −0.025 | 26.8 / 29.0 | 12 / 12 | 0.72 |
| 3 | 720 | 1,043 | −0.427 | −0.424 | −0.003 | 26.9 / 27.2 | 12 / 12 | 0.97 |
| 4 | 678 | 1,055 | −0.338 | −0.419 | +0.081 | 28.5 / 26.2 | 12 / 12 | 0.22 |
| 5 | 699 | 1,023 | −0.419 | −0.476 | +0.057 | 25.0 / 25.1 | 12 / 12 | 0.45 |

The bandit trades ~30 % less than the default because it keeps drifting to `k_surge = 4` (rarer, larger
surges). Posterior on all data — best arm per regime (posterior mean net R, n):

| regime | best arm | mean R | n | runner-up |
|---|---|---|---|---|
| vol_high · us | k4_n12_sl1.5 | −0.125 | 569 | k4_n6_sl1.5 −0.139 |
| vol_high · asia | k4_n24_sl1.5 | −0.179 | 451 | k4_n6_sl1.5 −0.196 |
| vol_low · late | k4_n24_sl1.5 | −0.202 | 70 | k4_n24_sl1 −0.202 |
| vol_high · eu | k4_n6_sl1.5 | −0.260 | 327 | k2.5_n6_sl1.5 −0.287 |
| vol_high · late | k3_n6_sl1.5 | −0.262 | 258 | k3_n12_sl1.5 −0.274 |
| vol_mid · us | k4_n6_sl1.5 | −0.360 | 644 | k4_n24_sl1.5 −0.378 |
| vol_mid · eu | k4_n24_sl1.5 | −0.366 | 687 | k4_n12_sl1.5 −0.383 |
| vol_mid · late | k4_n24_sl1.5 | −0.403 | 300 | k3_n24_sl1.5 −0.411 |
| vol_mid · asia | k4_n12_sl1.5 | −0.458 | 817 | k4_n6_sl1.5 −0.478 |
| vol_low · us | k2_n24_sl1 | −0.569 | 179 | k2_n24_sl1.5 −0.575 |
| vol_low · eu | k4_n24_sl1.5 | −0.611 | 189 | k4_n24_sl1 −0.612 |
| vol_low · asia | k2.5_n6_sl1.5 | −0.736 | 287 | k2.5_n6_sl1 −0.738 |

Two things in that table are more useful than the bandit itself: (a) the regime effect dwarfs the arm
effect — high-vol US/Asia sessions lose ~0.13–0.18 R per trade, low-vol Asia/EU lose ~0.6–0.7 R, whichever
parameters are used, so **a regime gate (do not trade low-vol sessions) is the obvious next experiment**;
(b) within a regime the top arms are within ~0.02 R of each other, which is well inside the noise at
these n. The default arm is never the posterior-best arm in any regime, but that is a 1-in-24 event
under the null anyway.

**Not recommended for live use.** The deployable variant (Thompson sampling rather than the greedy
posterior mean, with 12 regime cells × 24 arms) needs far more trades per cell than 475 days provide.

## Caveats (read before believing anything above)

1. **The entries lose money.** Every episode comes from CAN2 *test* parameters that average −0.45 R net
   over this period, with ~0.38 R of that being fees. An exit policy can only redistribute a negative
   outcome; a bandit over parameter sets can only pick the least-bad set. Nothing here is a tradable edge.
2. **REST candles, not tape.** Both experiments run on Delta's 1m REST backfill. Three of the sixteen
   observation dims (`buy_ratio`, `vpin_fast`, `kyle_bps`) are constants, and the bars can differ from
   the tape-built bars the live engine trades on. The replay path exists precisely to close this gap; it
   has ~90 minutes of real tape so far.
3. **Simulator, not the backtester.** Exit-policy numbers come from `ExitEnv` (one position, fixed 1 bp
   slippage, fills at next open or at the stop, no funding, no impact, no partial fills, no breakers) —
   the same simulator that generated the training data. The backtester hook (`exit_policy=`) is wired
   and tested but the headline table was not produced through it.
4. **One-bar lag.** Actions are applied at bar close; the live `ExitEngine` ratchets on every mark tick.
   The hand ladder in the environment is therefore slightly worse than live, which flatters the learned
   policy, not the ladder.
5. **Low-capacity learner, no tuning.** FQI with a quadratic ridge model, one seed, hyper-parameters set
   once and never tuned. Behaviour data is ladder / ε-ladder / random, so support far from the ladder's
   trajectory is thin (`exit_now`: 2,866 of 192,294 transitions) and Q extrapolates there.
6. **Statistics.** Expanding folds train on more data as they go; six unadjusted p-values per experiment;
   the permutation tests block by UTC day and assume days are exchangeable; the bandit comparison is
   unpaired because different arms trade at different times.
7. **Bandit evaluation is frozen per fold.** The posterior is fitted on the training window and the arm
   per regime is chosen greedily on the posterior mean; it is not updated inside the test window. Arms
   are backtested independently with research limits (breakers off, fixed 0.1 % risk), so the "policy"
   trade set is a union of per-arm backtests, not one portfolio.
8. **Sample sizes** are given next to every number; anything with n < ~500 trades per cell is noise.
