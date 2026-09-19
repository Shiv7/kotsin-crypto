from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from kotsin_crypto.bars.unified import UnifiedBar
from kotsin_crypto.research.backtest import (
    BacktestConfig,
    BacktestRunner,
    ProductSpec,
    config_from_dict,
    config_to_dict,
)
from kotsin_crypto.research.env import ACTION_INDEX, OBS_COLUMNS
from kotsin_crypto.research.eval import (
    paired_permutation_within_day,
    sign_flip_test,
    unpaired_permutation_within_day,
    walk_forward_folds,
)
from kotsin_crypto.research.rl.bandit import (
    DEFAULT_ARM,
    ContextualThompson,
    arm_name,
    attribute_regime,
)
from kotsin_crypto.research.rl.exit_policy import Transitions, collect_episodes, fqi
from kotsin_crypto.research.rl.policies import LearnedExitPolicy, policy_by_name
from kotsin_crypto.strategy.can2 import Can2Config

T0 = 1_780_000_000 - 1_780_000_000 % 28_800


def _obs(r: float) -> np.ndarray:
    o = np.zeros(len(OBS_COLUMNS))
    o[OBS_COLUMNS.index("r_now")] = r
    return o


def test_fqi_learns_to_exit_when_holding_only_loses() -> None:
    """Toy MDP: exit_now pays the current R; hold drifts R down by 0.5 for zero reward. Optimal: exit."""
    rng = np.random.default_rng(0)
    tr = Transitions()
    for _ in range(600):
        r = float(rng.uniform(-1.5, 2.0))
        if rng.random() < 0.5:
            tr.obs.append(_obs(r))
            tr.action.append(ACTION_INDEX["exit_now"])
            tr.reward.append(r)
            tr.next_obs.append(_obs(r))
            tr.done.append(True)
        else:
            nr = r - 0.5
            tr.obs.append(_obs(r))
            tr.action.append(ACTION_INDEX["hold"])
            tr.reward.append(-0.5 if nr > -2 else -2.0 - r)
            tr.next_obs.append(_obs(nr))
            tr.done.append(nr <= -2)
    pol = fqi(tr, n_iter=30)
    for r in (1.5, 0.5, -0.5):
        assert pol.act(_obs(r)) == ACTION_INDEX["exit_now"], r
    q = pol.q_values(_obs(1.0))
    assert q[ACTION_INDEX["lock_1r"]] < -1e5  # unsupported actions are never chosen
    art = pol.to_artefact()
    again = LearnedExitPolicy.from_artefact(json.loads(json.dumps(art)))
    assert again.act(_obs(1.0)) == ACTION_INDEX["exit_now"]


def test_bandit_picks_the_best_arm_per_context_and_respects_min_n() -> None:
    b = ContextualThompson(["a", "b", "c"], noise_var=1.0)
    rng = np.random.default_rng(1)
    for _ in range(60):
        b.update("hi", "a", rng.normal(1.0, 0.5))
        b.update("hi", "b", rng.normal(0.0, 0.5))
        b.update("hi", "c", rng.normal(-1.0, 0.5))
    assert b.choose("hi", default="b") == "a"
    assert b.choose("unseen", default="b") == "b"
    mean, var, n = b.posterior("hi", "a")
    assert 0.8 < mean < 1.2 and var < 0.05 and n == 60
    assert arm_name(DEFAULT_ARM) == "k2.5_n12_sl1.5"
    assert attribute_regime({"opened_ts": 1_000_060}, {999_900: "vol_low|asia"}) == "vol_low|asia"


def test_walk_forward_and_permutation_tests() -> None:
    folds = walk_forward_folds(0, 8000, n_folds=6)
    assert (
        len(folds) == 6 and folds[0].train_end == folds[0].test_start and folds[-1].test_end == 8000
    )
    assert all(f.train_start == 0 for f in folds) and folds[1].train_end > folds[0].train_end
    rng = np.random.default_rng(2)
    days = np.repeat(np.arange(40), 5)
    strong = rng.normal(0.5, 0.3, 200)
    assert sign_flip_test(strong, days, n_perm=500)["p_value"] < 0.01
    noise = rng.normal(0.0, 1.0, 200)
    assert paired_permutation_within_day(noise, days, n_perm=500)["p_value"] > 0.05
    up = unpaired_permutation_within_day(strong, days, noise, days, n_perm=300)
    assert up["n_a"] == 200 and up["p_value"] < 0.05


def _bars(symbol: str, n_min: int, event_at: int | None = None) -> list[UnifiedBar]:
    out = []
    px = 100.0
    for i in range(n_min):
        ts = T0 + i * 60
        vol, o, h, lo, c = 20.0, px, px + 0.05, px - 0.05, px
        if event_at is not None and event_at <= i < event_at + 5:
            vol, c = 400.0, px + 0.6
            h = c + 0.1
        px = c
        out.append(
            UnifiedBar(
                symbol=symbol,
                tf="1m",
                ts=ts,
                open=o,
                high=max(h, o, c),
                low=min(lo, o, c),
                close=c,
                volume=vol,
                has_trades=True,
                trade_count=int(vol),
                source="rest",
            )
        )
    return out


PRODUCTS = {"BTCUSD": ProductSpec(0.001, 0.25)}
PARAMS = {
    "k_surge": 2.0,
    "median_window": 10,
    "n_lookback": 6,
    "vwap_window": 6,
    "atr_window": 5,
    "cooldown_bars": 2,
}


def _run(bars: list[UnifiedBar], **over: object) -> dict:
    cfg = BacktestConfig(
        symbols=("BTCUSD",), start=bars[0].ts, end=bars[-1].ts + 60, params=PARAMS, **over
    )  # type: ignore[arg-type]
    return BacktestRunner(cfg, PRODUCTS, {}).run({"BTCUSD": bars})


def _strip(trades: list[dict]) -> list[dict]:
    return [{k: v for k, v in t.items() if k not in ("id", "position_id")} for t in trades]


def test_backtester_default_unchanged_and_policy_hook_works(tmp_path: Path) -> None:
    bars = _bars("BTCUSD", 900, event_at=300)
    a, b = _run(bars), _run(bars, exit_policy=None)
    assert _strip(a["trades"]) == _strip(b["trades"]) and a["stats"] == b["stats"]
    assert (
        a["stats"]["trades"] == 1
        and a["trades"][0]["r_unit"] > 0
        and a["trades"][0]["contract_value"] == 0.001
    )
    ladder = _run(bars, exit_policy="ladder")
    hold = _run(bars, exit_policy="hold_only")
    assert ladder["stats"]["trades"] >= 1 and hold["stats"]["trades"] >= 1
    assert ladder["trades"][0]["exit_reason"] in ("STOP", "TIME_STOP", "END", "POLICY")
    # a learned artefact loads through the same hook
    pol = LearnedExitPolicy(
        np.zeros((9, 2 * len(OBS_COLUMNS) + 1)),
        np.zeros(len(OBS_COLUMNS)),
        np.ones(len(OBS_COLUMNS)),
    )
    pol.weights[ACTION_INDEX["exit_now"], 0] = 1.0  # always exit at the first policy step
    path = tmp_path / "p.json"
    path.write_text(json.dumps(pol.to_artefact()))
    learned = _run(bars, exit_policy=str(path))
    assert learned["stats"]["trades"] == 1 and learned["trades"][0]["exit_reason"] == "POLICY"
    d = config_to_dict(BacktestConfig(symbols=("BTCUSD",), start=0, end=1, exit_policy="ladder"))
    assert d["exit_policy"] == "ladder" and config_from_dict(d).exit_policy == "ladder"
    assert policy_by_name("ladder").name == "ladder"


def test_collect_episodes_from_real_signals() -> None:
    from kotsin_crypto.research.features import resample_5m

    bars5 = resample_5m("BTCUSD", _bars("BTCUSD", 900, event_at=300))
    eps = collect_episodes("BTCUSD", bars5, Can2Config(**PARAMS))
    assert len(eps) == 1 and eps[0].side == 1 and eps[0].r_unit > 0 and eps[0].entry_index == 61


def test_dataset_npz_roundtrip_feeds_fqi(tmp_path: Path) -> None:
    from kotsin_crypto.research.env import ACTIONS
    from kotsin_crypto.research.features import resample_5m
    from kotsin_crypto.research.rl.exit_policy import collect_transitions, save_transitions

    bars5 = resample_5m("BTCUSD", _bars("BTCUSD", 900, event_at=300))
    eps = collect_episodes("BTCUSD", bars5, Can2Config(**PARAMS))
    tr = collect_transitions({"BTCUSD": bars5}, eps, seed=0, max_bars=12)
    assert len(tr) >= 3  # three behaviour policies × at least one step each
    path = tmp_path / "t.npz"
    save_transitions(tr, path)
    d = np.load(path)
    assert list(d["obs_columns"]) == list(OBS_COLUMNS) and list(d["actions"]) == list(ACTIONS)
    assert d["obs"].shape == (len(tr), len(OBS_COLUMNS)) and d["done"].sum() == 3
    pol = fqi(
        {k: d[k] for k in ("obs", "action", "reward", "next_obs", "done")}, n_iter=2, min_support=1
    )
    assert pol.meta["n_transitions"] == len(tr)
