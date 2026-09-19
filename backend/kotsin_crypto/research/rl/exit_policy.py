"""Offline RL for exits.

1. Episodes come from REAL CAN2 signals over history (the strategy runs on 5m bars exactly as in the
   backtester; each signal becomes an ExitEpisode filled at the next bar's open + slippage).
2. Behaviour policies (ladder, ε-ladder, random) roll the episodes through ExitEnv to log
   (obs, action, reward, next_obs, done) transitions.
3. Fitted Q-Iteration with a ridge-regressed quadratic approximator learns Q(s, a); the greedy policy
   is evaluated OUT OF SAMPLE per walk-forward fold against the ladder on the same episodes (paired,
   day-blocked permutation test) and under cost stress.

CLI:  python -m kotsin_crypto.research.rl.exit_policy run --symbols BTCUSD,ETHUSD --start 2025-06-01 --end 2026-09-19
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ...bars.unified import UnifiedBar
from ...strategy.base import Context, Side
from ...strategy.can2 import Can2, Can2Config
from ..data import load_cached_1m, parse_day
from ..env import ACTIONS, OBS_COLUMNS, ExitEnv, ExitEpisode
from ..eval import (
    cost_stress_variants,
    day_of,
    paired_permutation_within_day,
    trade_metrics,
    walk_forward_folds,
)
from ..features import resample_5m
from .policies import (
    EpsilonLadderPolicy,
    ExitPolicy,
    LearnedExitPolicy,
    RandomExitPolicy,
    RLadderExitPolicy,
    phi,
)

CONTRACT_VALUE = {"BTCUSD": 0.001, "ETHUSD": 0.01, "SOLUSD": 1.0}


class _ListCtx:
    """Strategy context over a growing prefix of a bar list."""

    def __init__(self, bars: Sequence[UnifiedBar]) -> None:
        self._bars = bars
        self.i = 0
        self.state: dict[str, object] = {}

    def bars(self, symbol: str, tf: str, n: int) -> list[UnifiedBar]:
        lo = max(0, self.i + 1 - n)
        return list(self._bars[lo : self.i + 1])


def collect_episodes(
    symbol: str,
    bars5: Sequence[UnifiedBar],
    cfg: Can2Config | None = None,
    *,
    fee_rate: float = 0.0005,
    slip_bps: float = 1.0,
) -> list[ExitEpisode]:
    strat = Can2(cfg or Can2Config())
    ctx: Context = _ListCtx(bars5)  # type: ignore[assignment]
    out: list[ExitEpisode] = []
    for i, bar in enumerate(bars5):
        ctx.i = i  # type: ignore[attr-defined]
        for sig in strat.on_bar(ctx, bar):
            if i + 1 >= len(bars5):
                continue
            side = 1 if sig.side is Side.LONG else -1
            fill = bars5[i + 1].open * (1 + side * slip_bps / 1e4)
            dist = abs(float(sig.entry) - float(sig.stop))
            if dist <= 0:
                continue
            out.append(
                ExitEpisode(
                    symbol=symbol,
                    side=side,
                    entry=fill,
                    r_unit=dist,
                    entry_index=i + 1,
                    contract_value=CONTRACT_VALUE.get(symbol, 0.001),
                    fee_rate=fee_rate,
                    slip_bps=slip_bps,
                    signal_ts=bar.ts,
                )
            )
    return out


@dataclass(slots=True)
class EpisodeResult:
    symbol: str
    signal_ts: int
    day: int
    net_r: float
    fees_r: float
    bars_held: int
    reason: str
    policy: str


@dataclass(slots=True)
class Transitions:
    obs: list[np.ndarray] = field(default_factory=list)
    action: list[int] = field(default_factory=list)
    reward: list[float] = field(default_factory=list)
    next_obs: list[np.ndarray] = field(default_factory=list)
    done: list[bool] = field(default_factory=list)

    def arrays(self) -> dict[str, np.ndarray]:
        return {
            "obs": np.asarray(self.obs, dtype=float),
            "action": np.asarray(self.action, dtype=int),
            "reward": np.asarray(self.reward, dtype=float),
            "next_obs": np.asarray(self.next_obs, dtype=float),
            "done": np.asarray(self.done, dtype=bool),
        }

    def __len__(self) -> int:
        return len(self.action)


def run_episode(
    bars5: Sequence[UnifiedBar],
    ep: ExitEpisode,
    policy: ExitPolicy,
    *,
    max_bars: int = 48,
    log: Transitions | None = None,
) -> EpisodeResult:
    env = ExitEnv(bars5, ep, max_bars=max_bars)
    obs = env.reset()
    while not env.done:
        a = policy.act(obs)
        res = env.step(a)
        if log is not None:
            log.obs.append(obs)
            log.action.append(a)
            log.reward.append(res.reward)
            log.next_obs.append(res.obs)
            log.done.append(res.done)
        obs = res.obs
    return EpisodeResult(
        ep.symbol,
        ep.signal_ts,
        day_of(ep.signal_ts),
        env.net_r,
        env.fees_r,
        env.bars_held,
        env.exit_reason,
        policy.name,
    )


def collect_transitions(
    bars5_by_symbol: dict[str, Sequence[UnifiedBar]],
    episodes: Sequence[ExitEpisode],
    *,
    seed: int = 0,
    max_bars: int = 48,
) -> Transitions:
    log = Transitions()
    behaviours: list[ExitPolicy] = [
        RLadderExitPolicy(),
        EpsilonLadderPolicy(0.3, seed),
        RandomExitPolicy(seed + 1),
    ]
    for ep in episodes:
        for pol in behaviours:
            run_episode(bars5_by_symbol[ep.symbol], ep, pol, max_bars=max_bars, log=log)
    return log


def fqi(
    tr: Transitions | dict[str, np.ndarray],
    *,
    n_iter: int = 40,
    gamma: float = 0.99,
    ridge: float = 1.0,
    min_support: int = 25,
) -> LearnedExitPolicy:
    d = tr.arrays() if isinstance(tr, Transitions) else tr
    obs, act, rew, nxt, done = (
        d["obs"],
        d["action"],
        d["reward"],
        d["next_obs"],
        d["done"].astype(float),
    )
    mu = obs.mean(axis=0)
    sigma = obs.std(axis=0)
    sigma = np.where(sigma > 1e-9, sigma, 1.0)
    P = phi((obs - mu) / sigma)
    Pn = phi((nxt - mu) / sigma)
    n_a, n_f = len(ACTIONS), P.shape[1]
    W = np.zeros((n_a, n_f))
    support = np.array([(act == a).sum() for a in range(n_a)])
    usable = support >= min_support
    reg = ridge * np.eye(n_f)
    for _ in range(n_iter):
        q_next = Pn @ W.T  # (N, A)
        q_next[:, ~usable] = -np.inf
        target = rew + gamma * (1.0 - done) * np.max(q_next, axis=1)
        target = np.where(np.isfinite(target), target, rew)
        for a in range(n_a):
            if not usable[a]:
                continue
            m = act == a
            X, y = P[m], target[m]
            W[a] = np.linalg.solve(X.T @ X + reg, X.T @ y)
    W[~usable, :] = 0.0
    W[~usable, 0] = -1e6  # never choose an action the data cannot support
    meta = {
        "n_transitions": len(act),
        "support": support.tolist(),
        "n_iter": n_iter,
        "gamma": gamma,
        "ridge": ridge,
        "trained_at": time.time(),
    }
    return LearnedExitPolicy(W, mu, sigma, meta)


def evaluate(
    bars5_by_symbol: dict[str, Sequence[UnifiedBar]],
    episodes: Sequence[ExitEpisode],
    policy: ExitPolicy,
    *,
    max_bars: int = 48,
) -> list[EpisodeResult]:
    return [
        run_episode(bars5_by_symbol[ep.symbol], ep, policy, max_bars=max_bars) for ep in episodes
    ]


def _summ(res: Sequence[EpisodeResult]) -> dict[str, Any]:
    return trade_metrics(
        [r.net_r for r in res], [r.bars_held * 300 for r in res], [r.fees_r for r in res]
    )


def experiment(
    symbols: Sequence[str],
    start: int,
    end: int,
    *,
    history_root: Path,
    out_dir: Path,
    n_folds: int = 6,
    seed: int = 0,
    max_bars: int = 48,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    t0 = time.time()
    cfg = Can2Config(**(params or {}))
    bars5_by_symbol: dict[str, list[UnifiedBar]] = {}
    episodes: list[ExitEpisode] = []
    for sym in symbols:
        b1 = load_cached_1m(sym, start, end, history_root)
        if not b1:
            continue
        b5 = resample_5m(sym, b1)
        bars5_by_symbol[sym] = b5
        episodes.extend(collect_episodes(sym, b5, cfg))
    episodes.sort(key=lambda e: e.signal_ts)
    folds = walk_forward_folds(start, end, n_folds=n_folds)
    ladder = RLadderExitPolicy()
    fold_rows: list[dict[str, Any]] = []
    for fold in folds:
        train = [e for e in episodes if fold.in_train(e.signal_ts)]
        test = [e for e in episodes if fold.in_test(e.signal_ts)]
        row: dict[str, Any] = {
            "fold": fold.index,
            "train_episodes": len(train),
            "test_episodes": len(test),
            "test_start": fold.test_start,
            "test_end": fold.test_end,
        }
        if len(train) < 30 or len(test) < 5:
            row["skipped"] = "too few episodes"
            fold_rows.append(row)
            continue
        tr = collect_transitions(bars5_by_symbol, train, seed=seed + fold.index, max_bars=max_bars)
        learned = fqi(tr)
        res_l = evaluate(bars5_by_symbol, test, learned, max_bars=max_bars)
        res_b = evaluate(bars5_by_symbol, test, ladder, max_bars=max_bars)
        diffs = [a.net_r - b.net_r for a, b in zip(res_l, res_b, strict=True)]
        pt = paired_permutation_within_day(diffs, [r.day for r in res_l], seed=seed)
        ml, mb = _summ(res_l), _summ(res_b)
        row.update(
            n_policy=ml["n"],
            n_transitions=len(tr),
            mean_net_r_policy=ml["mean_net_r"],
            mean_net_r_baseline=mb["mean_net_r"],
            win_rate_policy=ml["win_rate"],
            win_rate_baseline=mb["win_rate"],
            fees_r_policy=ml["fees_r_per_trade"],
            fees_r_baseline=mb["fees_r_per_trade"],
            avg_hold_bars_policy=(ml["avg_hold_s"] or 0) / 300,
            avg_hold_bars_baseline=(mb["avg_hold_s"] or 0) / 300,
            exits_policy=_reasons(res_l),
            exits_baseline=_reasons(res_b),
            paired_p_value=pt["p_value"],
            action_support=learned.meta["support"],
        )
        stress: dict[str, Any] = {}
        for name, (fee, slip) in cost_stress_variants(0.0005, 1.0).items():
            if name == "base":
                continue
            eps_s = [
                replace(
                    e,
                    fee_rate=fee,
                    slip_bps=slip,
                    entry=e.entry * (1 + e.side * (slip - e.slip_bps) / 1e4),
                )
                for e in test
            ]
            sl = _summ(evaluate(bars5_by_symbol, eps_s, learned, max_bars=max_bars))
            sb = _summ(evaluate(bars5_by_symbol, eps_s, ladder, max_bars=max_bars))
            stress[name] = {"policy": sl["mean_net_r"], "baseline": sb["mean_net_r"]}
        row["cost_stress"] = stress
        fold_rows.append(row)
    # final policy on everything (for deployment experiments only; not evaluated here)
    final = (
        fqi(collect_transitions(bars5_by_symbol, episodes, seed=seed, max_bars=max_bars))
        if len(episodes) >= 30
        else None
    )
    valid = [r for r in fold_rows if "skipped" not in r]
    summary = {
        "symbols": list(bars5_by_symbol),
        "episodes": len(episodes),
        "episodes_by_symbol": {
            s: sum(1 for e in episodes if e.symbol == s) for s in bars5_by_symbol
        },
        "n_folds_evaluated": len(valid),
        "mean_net_r_policy": float(np.mean([r["mean_net_r_policy"] for r in valid]))
        if valid
        else None,
        "mean_net_r_baseline": float(np.mean([r["mean_net_r_baseline"] for r in valid]))
        if valid
        else None,
        "folds_policy_beats_baseline": sum(
            1 for r in valid if r["mean_net_r_policy"] > r["mean_net_r_baseline"]
        ),
        "pooled_p_values": [r["paired_p_value"] for r in valid],
        "wall_clock_s": round(time.time() - t0, 1),
    }
    name = f"exit_policy_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M')}"
    artefact = {
        "kind": "exit_policy",
        "name": name,
        "created_ts": time.time(),
        "config": {
            "symbols": list(symbols),
            "start": start,
            "end": end,
            "n_folds": n_folds,
            "seed": seed,
            "max_bars": max_bars,
            "can2": params or {},
            "actions": list(ACTIONS),
            "obs_columns": OBS_COLUMNS,
        },
        "summary": summary,
        "folds": fold_rows,
        "policy": final.to_artefact() if final else None,
        "caveats": [
            "Episodes come from CAN2 test parameters that lose money on average; the exit policy can only redistribute that outcome.",
            "The environment applies actions at bar close with a one-bar lag versus the live ratchet, which tightens on every mark tick.",
            "Fills at next-bar open with fixed slippage; no book impact. Backfilled bars carry no microstructure, so buy_ratio/vpin/kyle are constants for the policy here.",
            "Walk-forward means later folds train on more data; fold p-values are not adjusted for multiple comparisons.",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.json").write_text(json.dumps(artefact, default=str))
    return artefact


def _reasons(res: Sequence[EpisodeResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in res:
        out[r.reason] = out.get(r.reason, 0) + 1
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--symbols", default="BTCUSD,ETHUSD")
    run.add_argument("--start", default="2025-06-01")
    run.add_argument("--end", default="2026-09-19")
    run.add_argument("--history", default="data/history")
    run.add_argument("--out", default="data/rl")
    run.add_argument("--folds", type=int, default=6)
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--k-surge", type=float, default=2.5)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        art = experiment(
            a.symbols.split(","),
            parse_day(a.start),
            parse_day(a.end) + 86_400,
            history_root=Path(a.history),
            out_dir=Path(a.out),
            n_folds=a.folds,
            seed=a.seed,
            params={"k_surge": a.k_surge},
        )
        print(json.dumps(art["summary"], indent=1))
        for r in art["folds"]:
            print(
                {
                    k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in r.items()
                    if k
                    in (
                        "fold",
                        "train_episodes",
                        "test_episodes",
                        "mean_net_r_policy",
                        "mean_net_r_baseline",
                        "paired_p_value",
                        "skipped",
                    )
                }
            )


if __name__ == "__main__":
    main()
