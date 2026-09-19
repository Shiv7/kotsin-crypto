"""Contextual Thompson-sampling bandit over CAN2 parameter sets, with the regime (volatility tercile ×
session) as context. Rewards are the net R of trades opened in a regime by an arm, from full
backtester runs (breakers disabled so every signal is observed). Evaluated walk-forward: posteriors
are fitted on the training window, the chosen arm per regime is applied on the test window by taking
that arm's test-window trades in that regime, and compared with the fixed default arm (unpaired,
day-blocked permutation test).

CLI:  python -m kotsin_crypto.research.rl.bandit run --symbols BTCUSD,ETHUSD --start 2025-06-01 --end 2026-09-19
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ...bars.unified import UnifiedBar
from ..backtest import BacktestConfig, BacktestRunner, ProductSpec
from ..data import load_cached_1m, parse_day
from ..eval import day_of, trade_metrics, unpaired_permutation_within_day, walk_forward_folds
from ..features import build_frame, regime_of, resample_5m

ARM_GRID: list[tuple[float, int, float]] = [
    (k, n, s) for k, n, s in itertools.product((2.0, 2.5, 3.0, 4.0), (6, 12, 24), (1.0, 1.5))
]
DEFAULT_ARM: tuple[float, int, float] = (2.5, 12, 1.5)
PRODUCTS = {
    "BTCUSD": ProductSpec(0.001, 0.25),
    "ETHUSD": ProductSpec(0.01, 0.25),
    "SOLUSD": ProductSpec(1.0, 0.5),
}
RESEARCH_LIMITS = {
    "daily_loss_limit_pct": 1e9,
    "max_drawdown_pct": 1e9,
    "max_positions_total": 3,
    "risk_per_trade_pct": 0.1,
}


def arm_name(arm: tuple[float, int, float]) -> str:
    return f"k{arm[0]:g}_n{arm[1]}_sl{arm[2]:g}"


def arm_params(arm: tuple[float, int, float]) -> dict[str, Any]:
    return {"k_surge": arm[0], "n_lookback": arm[1], "sl_atr_mult": arm[2]}


def run_arm(
    symbol: str, bars_1m: Sequence[UnifiedBar], arm: tuple[float, int, float], start: int, end: int
) -> list[dict[str, Any]]:
    cfg = BacktestConfig(
        symbols=(symbol,),
        start=start,
        end=end,
        params=arm_params(arm),
        initial_usd=100_000.0,
        limits=RESEARCH_LIMITS,
        apply_funding=False,
    )
    result = BacktestRunner(cfg, PRODUCTS, {}).run({symbol: bars_1m})
    out = []
    for t in result["trades"]:
        denom = t["contracts"] * t["contract_value"] * t["r_unit"] if t.get("r_unit") else 0.0
        if denom <= 0:
            continue
        out.append(
            {
                "symbol": symbol,
                "opened_ts": t["opened_ts"],
                "net_r": t["net"] / denom,
                "net": t["net"],
                "fees": t["fees"],
                "day": day_of(t["opened_ts"]),
            }
        )
    return out


def regime_lookup(symbol: str, bars5: Sequence[UnifiedBar]) -> dict[int, str]:
    f = build_frame(symbol, bars5)
    out: dict[int, str] = {}
    for i in range(len(bars5)):
        row = {
            "rv_48": float(f["rv_48"][i]),
            "hour_sin": float(f["hour_sin"][i]),
            "hour_cos": float(f["hour_cos"][i]),
        }
        out[int(f["ts"][i])] = regime_of(row)
    return out


def attribute_regime(trade: dict[str, Any], lookup: dict[int, str]) -> str:
    """Decision bar = the 5m bar that closed just before the fill (fill is at the next 1m open)."""
    ts = int(trade["opened_ts"])
    decision = ts - (ts % 300) - 300 if ts % 300 == 0 else ts - (ts % 300)
    return lookup.get(decision) or lookup.get(decision - 300) or "unknown"


class ContextualThompson:
    """Independent Gaussian posteriors on the mean reward per (context, arm); N(0, 1) prior, noise
    variance from the pooled reward variance (floored)."""

    def __init__(self, arms: Sequence[str], noise_var: float = 1.0, prior_var: float = 1.0) -> None:
        self.arms = list(arms)
        self.noise_var = max(noise_var, 1e-3)
        self.prior_var = prior_var
        self.stats: dict[tuple[str, str], tuple[int, float]] = {}  # (ctx, arm) → (n, sum)

    def update(self, ctx: str, arm: str, reward: float) -> None:
        n, s = self.stats.get((ctx, arm), (0, 0.0))
        self.stats[(ctx, arm)] = (n + 1, s + reward)

    def posterior(self, ctx: str, arm: str) -> tuple[float, float, int]:
        n, s = self.stats.get((ctx, arm), (0, 0.0))
        prec = 1 / self.prior_var + n / self.noise_var
        mean = (s / self.noise_var) / prec
        return mean, 1 / prec, n

    def choose(
        self,
        ctx: str,
        rng: np.random.Generator | None = None,
        *,
        min_n: int = 10,
        default: str | None = None,
    ) -> str:
        best, best_v = default or self.arms[0], -np.inf
        for arm in self.arms:
            mean, var, n = self.posterior(ctx, arm)
            if n < min_n:
                continue
            v = rng.normal(mean, np.sqrt(var)) if rng is not None else mean
            if v > best_v:
                best, best_v = arm, v
        return best


def experiment(
    symbols: Sequence[str],
    start: int,
    end: int,
    *,
    history_root: Path,
    out_dir: Path,
    n_folds: int = 6,
    seed: int = 0,
    arms: Sequence[tuple[float, int, float]] = ARM_GRID,
) -> dict[str, Any]:
    t0 = time.time()
    trades_by_arm: dict[str, list[dict[str, Any]]] = {arm_name(a): [] for a in arms}
    lookups: dict[str, dict[int, str]] = {}
    runs = 0
    for sym in symbols:
        b1 = load_cached_1m(sym, start, end, history_root)
        if not b1:
            continue
        lookups[sym] = regime_lookup(sym, resample_5m(sym, b1))
        for arm in arms:
            for t in run_arm(sym, b1, arm, start, end):
                t["regime"] = attribute_regime(t, lookups[sym])
                trades_by_arm[arm_name(arm)].append(t)
            runs += 1
    default = arm_name(DEFAULT_ARM)
    all_r = [t["net_r"] for ts in trades_by_arm.values() for t in ts]
    noise_var = float(np.var(all_r)) if len(all_r) > 2 else 1.0
    folds = walk_forward_folds(start, end, n_folds=n_folds)
    fold_rows: list[dict[str, Any]] = []
    for fold in folds:
        bandit = ContextualThompson(list(trades_by_arm), noise_var=noise_var)
        for arm, ts in trades_by_arm.items():
            for t in ts:
                if fold.in_train(t["opened_ts"]):
                    bandit.update(t["regime"], arm, t["net_r"])
        regimes = sorted(
            {
                t["regime"]
                for ts in trades_by_arm.values()
                for t in ts
                if fold.in_train(t["opened_ts"])
            }
        )
        chosen = {
            r: bandit.choose(r, None, default=default) for r in regimes
        }  # greedy on posterior mean for evaluation
        policy_trades = [
            t
            for r, arm in chosen.items()
            for t in trades_by_arm[arm]
            if fold.in_test(t["opened_ts"]) and t["regime"] == r
        ]
        base_trades = [t for t in trades_by_arm[default] if fold.in_test(t["opened_ts"])]
        mp, mb = (
            trade_metrics([t["net_r"] for t in policy_trades]),
            trade_metrics([t["net_r"] for t in base_trades]),
        )
        pt = unpaired_permutation_within_day(
            [t["net_r"] for t in policy_trades],
            [t["day"] for t in policy_trades],
            [t["net_r"] for t in base_trades],
            [t["day"] for t in base_trades],
            seed=seed,
        )
        fold_rows.append(
            {
                "fold": fold.index,
                "test_start": fold.test_start,
                "test_end": fold.test_end,
                "n_policy": mp["n"],
                "n_baseline": mb["n"],
                "mean_net_r_policy": mp["mean_net_r"],
                "mean_net_r_baseline": mb["mean_net_r"],
                "win_rate_policy": mp["win_rate"],
                "win_rate_baseline": mb["win_rate"],
                "chosen_arms": chosen,
                "changed_regimes": sum(1 for a in chosen.values() if a != default),
                "p_value": pt["p_value"],
            }
        )
    valid = [r for r in fold_rows if r["n_policy"] and r["n_baseline"]]
    posterior_table = {}
    bandit_all = ContextualThompson(list(trades_by_arm), noise_var=noise_var)
    for arm, ts in trades_by_arm.items():
        for t in ts:
            bandit_all.update(t["regime"], arm, t["net_r"])
    for r in sorted({t["regime"] for ts in trades_by_arm.values() for t in ts}):
        ranked = sorted(
            (
                (bandit_all.posterior(r, a)[0], a, bandit_all.posterior(r, a)[2])
                for a in trades_by_arm
            ),
            reverse=True,
        )[:3]
        posterior_table[r] = [
            {"arm": a, "posterior_mean": round(m, 4), "n": n} for m, a, n in ranked
        ]
    summary = {
        "symbols": list(lookups),
        "arms": len(arms),
        "backtest_runs": runs,
        "total_trades_all_arms": len(all_r),
        "trades_default_arm": len(trades_by_arm[default]),
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
        "p_values": [r["p_value"] for r in valid],
        "wall_clock_s": round(time.time() - t0, 1),
    }
    name = f"bandit_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M')}"
    artefact = {
        "kind": "bandit",
        "name": name,
        "created_ts": time.time(),
        "config": {
            "symbols": list(symbols),
            "start": start,
            "end": end,
            "n_folds": n_folds,
            "seed": seed,
            "arms": [arm_name(a) for a in arms],
            "default_arm": default,
            "limits": RESEARCH_LIMITS,
        },
        "summary": summary,
        "folds": fold_rows,
        "posterior_top3_by_regime": posterior_table,
        "caveats": [
            "Rewards are per-trade net R from breaker-free backtests with a 100k wallet and 0.1% risk per trade, so every signal is observed; live sizing and breakers will change the realised path.",
            "Arm selection per regime is greedy on the posterior mean at evaluation time; the deployable policy would sample (Thompson).",
            "Different arms produce different trade sets, so the comparison is unpaired; the day-blocked permutation p-value is the honest statistic.",
            "Regime labels come from fixed cut-offs on 48-bar realised vol and UTC session; they are not tuned.",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.json").write_text(json.dumps(artefact, default=str))
    return artefact


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
                        "n_policy",
                        "n_baseline",
                        "mean_net_r_policy",
                        "mean_net_r_baseline",
                        "changed_regimes",
                        "p_value",
                    )
                }
            )


if __name__ == "__main__":
    main()
