"""Evaluation harness shared by the RL experiments: walk-forward folds by time, cost stress variants,
day-blocked permutation tests (trades cluster by day, so a naive split is not exchangeable), and a
standard trade-metrics summary."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    train_start: int
    train_end: int  # exclusive, unix s
    test_start: int
    test_end: int

    def in_train(self, ts: float) -> bool:
        return self.train_start <= ts < self.train_end

    def in_test(self, ts: float) -> bool:
        return self.test_start <= ts < self.test_end


def walk_forward_folds(
    start: int, end: int, n_folds: int = 6, min_train_blocks: int = 2
) -> list[Fold]:
    """Expanding-window folds: the range is cut into ``n_folds + min_train_blocks`` equal blocks;
    fold k trains on blocks [0, k+min_train_blocks) and tests on the next block."""
    n_blocks = n_folds + min_train_blocks
    edges = [start + (end - start) * i // n_blocks for i in range(n_blocks + 1)]
    return [
        Fold(
            k,
            edges[0],
            edges[k + min_train_blocks],
            edges[k + min_train_blocks],
            edges[k + min_train_blocks + 1],
        )
        for k in range(n_folds)
    ]


def cost_stress_variants(fee_rate: float, slip_bps: float) -> dict[str, tuple[float, float]]:
    return {
        "base": (fee_rate, slip_bps),
        "fees_x1_5": (fee_rate * 1.5, slip_bps),
        "slip_x2": (fee_rate, slip_bps * 2),
        "both": (fee_rate * 1.5, slip_bps * 2),
    }


def day_of(ts: float) -> int:
    return int(ts // 86_400)


def trade_metrics(
    net_r: Sequence[float],
    hold_s: Sequence[float] | None = None,
    fees_r: Sequence[float] | None = None,
) -> dict[str, Any]:
    x = np.asarray(net_r, dtype=float)
    n = len(x)
    if n == 0:
        return {
            "n": 0,
            "mean_net_r": None,
            "median_net_r": None,
            "win_rate": None,
            "std_net_r": None,
            "se_net_r": None,
            "avg_hold_s": None,
            "fees_r_per_trade": None,
        }
    return {
        "n": n,
        "mean_net_r": float(x.mean()),
        "median_net_r": float(np.median(x)),
        "win_rate": float((x > 0).mean()),
        "std_net_r": float(x.std(ddof=1)) if n > 1 else None,
        "se_net_r": float(x.std(ddof=1) / math.sqrt(n)) if n > 1 else None,
        "avg_hold_s": float(np.mean(hold_s)) if hold_s is not None and len(hold_s) else None,
        "fees_r_per_trade": float(np.mean(fees_r)) if fees_r is not None and len(fees_r) else None,
    }


def sign_flip_test(
    values: Sequence[float], days: Sequence[int], n_perm: int = 4000, seed: int = 0
) -> dict[str, Any]:
    """H0: mean = 0. Day-blocked sign flips: all trades of a day flip together, so the test respects
    within-day clustering. Two-sided p-value."""
    x = np.asarray(values, dtype=float)
    d = np.asarray(days)
    if len(x) < 2:
        return {
            "n": len(x),
            "statistic": float(x.mean()) if len(x) else None,
            "p_value": None,
            "n_days": len(set(d.tolist())),
        }
    rng = np.random.default_rng(seed)
    uniq, inv = np.unique(d, return_inverse=True)
    stat = x.mean()
    count = 0
    for _ in range(n_perm):
        flips = rng.choice([-1.0, 1.0], size=len(uniq))
        if abs((x * flips[inv]).mean()) >= abs(stat) - 1e-15:
            count += 1
    return {
        "n": len(x),
        "n_days": len(uniq),
        "statistic": float(stat),
        "p_value": (count + 1) / (n_perm + 1),
    }


def paired_permutation_within_day(
    diffs: Sequence[float], days: Sequence[int], n_perm: int = 4000, seed: int = 0
) -> dict[str, Any]:
    """Paired comparison of two policies on the same episodes (diff = A − B per episode), day-blocked."""
    return sign_flip_test(diffs, days, n_perm=n_perm, seed=seed)


def unpaired_permutation_within_day(
    a: Sequence[float],
    a_days: Sequence[int],
    b: Sequence[float],
    b_days: Sequence[int],
    n_perm: int = 4000,
    seed: int = 0,
) -> dict[str, Any]:
    """Unpaired comparison (different trade sets): group labels are shuffled within each day."""
    if len(a) == 0 or len(b) == 0:
        return {"n_a": len(a), "n_b": len(b), "statistic": None, "p_value": None}
    x = np.concatenate([np.asarray(a, float), np.asarray(b, float)])
    g = np.concatenate([np.ones(len(a)), np.zeros(len(b))])
    d = np.concatenate([np.asarray(a_days), np.asarray(b_days)])
    stat = x[g == 1].mean() - x[g == 0].mean()
    rng = np.random.default_rng(seed)
    count = 0
    groups = [np.where(d == day)[0] for day in np.unique(d)]
    for _ in range(n_perm):
        gp = g.copy()
        for idx in groups:
            gp[idx] = rng.permutation(gp[idx])
        if gp.sum() == 0 or gp.sum() == len(gp):
            continue
        s = x[gp == 1].mean() - x[gp == 0].mean()
        if abs(s) >= abs(stat) - 1e-15:
            count += 1
    return {
        "n_a": len(a),
        "n_b": len(b),
        "statistic": float(stat),
        "p_value": (count + 1) / (n_perm + 1),
    }


def fold_report(
    name: str, folds: list[dict[str, Any]], notes: list[str] | None = None
) -> dict[str, Any]:
    """Aggregate per-fold dicts (each with n_policy, mean_net_r_policy, mean_net_r_baseline)."""
    ns = [f.get("n_policy", 0) or 0 for f in folds]
    means = [f["mean_net_r_policy"] for f in folds if f.get("mean_net_r_policy") is not None]
    base = [f["mean_net_r_baseline"] for f in folds if f.get("mean_net_r_baseline") is not None]
    wins = sum(
        1
        for f in folds
        if f.get("mean_net_r_policy") is not None
        and f.get("mean_net_r_baseline") is not None
        and f["mean_net_r_policy"] > f["mean_net_r_baseline"]
    )
    return {
        "name": name,
        "folds": folds,
        "summary": {
            "n_folds": len(folds),
            "total_trades_policy": int(sum(ns)),
            "mean_of_fold_means_policy": float(np.mean(means)) if means else None,
            "mean_of_fold_means_baseline": float(np.mean(base)) if base else None,
            "folds_policy_beats_baseline": wins,
        },
        "notes": notes or [],
    }
