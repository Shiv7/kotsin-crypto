from __future__ import annotations

import numpy as np

from kotsin_crypto.bars.unified import UnifiedBar
from kotsin_crypto.research.env import (
    ACTION_INDEX,
    OBS_COLUMNS,
    ExitEnv,
    ExitEpisode,
    compute_obs,
    stop_after_action,
)
from kotsin_crypto.research.rl.policies import RandomExitPolicy, RLadderExitPolicy

T0 = 1_700_000_000 - 1_700_000_000 % 300


def bar(i: int, o: float, h: float, lo: float, c: float, v: float = 100.0) -> UnifiedBar:
    return UnifiedBar(
        symbol="BTCUSD",
        tf="5m",
        ts=T0 + i * 300,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=v,
        has_trades=True,
    )


def flat(n: int, px: float = 100.0) -> list[UnifiedBar]:
    return [bar(i, px, px + 0.1, px - 0.1, px) for i in range(n)]


def long_ep(entry_index: int = 5, entry: float = 100.0, r_unit: float = 2.0) -> ExitEpisode:
    return ExitEpisode("BTCUSD", 1, entry, r_unit, entry_index, fee_rate=0.0005, slip_bps=1.0)


def test_gap_through_stop_fills_at_open() -> None:
    bars = [*flat(6), bar(6, 97.0, 97.5, 96.0, 96.5)]
    env = ExitEnv(bars, long_ep())
    env.reset()
    res = env.step(ACTION_INDEX["hold"])
    assert res.done and res.info["reason"] == "stop_gap"
    assert abs(env.exit_price - 97.0 * (1 - 1e-4)) < 1e-9
    assert res.info["net_r"] < -1.4  # -1.5R plus fees


def test_intrabar_stop_fills_at_stop() -> None:
    bars = [*flat(6), bar(6, 100.0, 100.5, 97.5, 99.0)]
    env = ExitEnv(bars, long_ep())
    env.reset()
    res = env.step(ACTION_INDEX["hold"])
    assert (
        res.done and res.info["reason"] == "stop" and abs(env.exit_price - 98.0 * (1 - 1e-4)) < 1e-9
    )


def test_time_stop_and_reward_telescopes_to_net_r() -> None:
    bars = flat(12)
    env = ExitEnv(bars, long_ep(), max_bars=3)
    first = env.net_r
    env.reset()
    total = 0.0
    while not env.done:
        total += env.step(ACTION_INDEX["hold"]).reward
    assert env.exit_reason == "time_stop" and env.bars_held == 3
    assert abs(total - (env.net_r - first)) < 1e-12


def test_actions_only_tighten_and_ladder_matches_engine_ladder() -> None:
    assert stop_after_action("lock_1r", side=1, entry=100, r_unit=2, stop=98, peak_r=3) == 102
    assert (
        stop_after_action("lock_0r", side=1, entry=100, r_unit=2, stop=102, peak_r=3) == 102
    )  # never loosens
    assert stop_after_action("trail_1_5r", side=-1, entry=100, r_unit=2, stop=102, peak_r=5) == 93
    assert stop_after_action("hold", side=1, entry=100, r_unit=2, stop=98, peak_r=3) == 98
    pol = RLadderExitPolicy()

    def obs_with_peak(p: float) -> np.ndarray:
        o = np.zeros(len(OBS_COLUMNS))
        o[OBS_COLUMNS.index("peak_r")] = p
        return o

    assert [pol.act(obs_with_peak(p)) for p in (0.5, 1.0, 2.5, 3.2, 4.1, 5.0)] == [
        ACTION_INDEX["hold"],
        ACTION_INDEX["lock_0r"],
        ACTION_INDEX["lock_1r"],
        ACTION_INDEX["lock_2r"],
        ACTION_INDEX["lock_3r"],
        ACTION_INDEX["trail_1_5r"],
    ]


def test_ratchet_locks_profit_after_run_up() -> None:
    # long from 100, r=2; price runs to 108 (peak 4R) then collapses: ladder should exit near +3R
    bars = [
        *flat(6),
        bar(6, 100, 104, 100, 104),
        bar(7, 104, 108, 104, 107),
        bar(8, 107, 107.5, 90, 91),
    ]
    env = ExitEnv(bars, long_ep())
    pol = RLadderExitPolicy()
    obs = env.reset()
    while not env.done:
        obs = env.step(pol.act(obs)).obs
    assert env.exit_reason == "stop" and 2.5 < env.net_r < 3.0


def test_short_side_symmetry_and_obs_shape() -> None:
    bars = [*flat(6), bar(6, 103.0, 104.0, 102.5, 103.5)]  # gaps up through a short's stop at 102
    ep = ExitEpisode("BTCUSD", -1, 100.0, 2.0, 5)
    env = ExitEnv(bars, ep)
    obs = env.reset()
    assert obs.shape == (len(OBS_COLUMNS),) and np.isfinite(obs).all()
    res = env.step(ACTION_INDEX["hold"])
    assert res.done and res.info["reason"] == "stop_gap" and res.info["net_r"] < -1.4


def test_random_policy_runs_to_completion_deterministically() -> None:
    bars = [
        bar(i, 100 + (i % 5) * 0.3, 100.6 + (i % 5) * 0.3, 99.6, 100.2 + (i % 5) * 0.3)
        for i in range(60)
    ]
    outs = []
    for _ in range(2):
        env = ExitEnv(bars, long_ep(entry_index=3), max_bars=40)
        pol = RandomExitPolicy(seed=3)
        obs = env.reset()
        while not env.done:
            obs = env.step(pol.act(obs)).obs
        outs.append((env.exit_reason, env.net_r, env.bars_held))
    assert outs[0] == outs[1]


def test_compute_obs_uses_window_features() -> None:
    bars = flat(60)
    o = compute_obs(side=1, entry=100.0, r_unit=2.0, peak_r=1.5, bars_held=4, window=bars[-49:])
    assert o[OBS_COLUMNS.index("peak_r")] == 1.5 and o[OBS_COLUMNS.index("bars_held")] == 4
    assert (
        abs(o[OBS_COLUMNS.index("r_now")]) < 1e-9
        and 0 <= o[OBS_COLUMNS.index("hours_to_funding")] <= 8
    )
