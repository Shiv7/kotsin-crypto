"""Exit policies over ``research.env.ACTIONS``: the hand ladder (baseline), behaviour policies for
offline data collection, and a learned policy loaded from an artefact JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ..env import ACTION_INDEX, ACTIONS, OBS_COLUMNS


class ExitPolicy(Protocol):
    name: str

    def act(self, obs: np.ndarray) -> int: ...


def phi(z: np.ndarray) -> np.ndarray:
    """Quadratic feature map on standardised observations: [1, z, z²] (2d+1 terms)."""
    z = np.asarray(z, dtype=float)
    if z.ndim == 1:
        return np.concatenate([[1.0], z, z * z])
    return np.concatenate([np.ones((z.shape[0], 1)), z, z * z], axis=1)


class RLadderExitPolicy:
    """Reproduces risk/exits.py: +1R → breakeven, +2R → lock +1R, +3R → lock +2R, +4R → lock +3R,
    beyond that trail 1.5R behind the peak (which overtakes the +3R lock past +4.5R)."""

    name = "ladder"

    def act(self, obs: np.ndarray) -> int:
        peak = float(obs[OBS_COLUMNS.index("peak_r")])
        if peak >= 4.5:
            return ACTION_INDEX["trail_1_5r"]
        if peak >= 4.0:
            return ACTION_INDEX["lock_3r"]
        if peak >= 3.0:
            return ACTION_INDEX["lock_2r"]
        if peak >= 2.0:
            return ACTION_INDEX["lock_1r"]
        if peak >= 1.0:
            return ACTION_INDEX["lock_0r"]
        return ACTION_INDEX["hold"]


class HoldToTimeStopPolicy:
    name = "hold_only"

    def act(self, obs: np.ndarray) -> int:
        return ACTION_INDEX["hold"]


class RandomExitPolicy:
    """Behaviour policy for data collection: mostly hold, sometimes a random tightening/exit."""

    name = "random"

    def __init__(self, seed: int = 0, p_hold: float = 0.6) -> None:
        self.rng = np.random.default_rng(seed)
        self.p_hold = p_hold

    def act(self, obs: np.ndarray) -> int:
        if self.rng.random() < self.p_hold:
            return ACTION_INDEX["hold"]
        return int(self.rng.integers(1, len(ACTIONS)))


class EpsilonLadderPolicy:
    name = "eps_ladder"

    def __init__(self, eps: float = 0.2, seed: int = 0) -> None:
        self.eps = eps
        self.rng = np.random.default_rng(seed)
        self.ladder = RLadderExitPolicy()

    def act(self, obs: np.ndarray) -> int:
        if self.rng.random() < self.eps:
            return int(self.rng.integers(0, len(ACTIONS)))
        return self.ladder.act(obs)


class LearnedExitPolicy:
    """Greedy over Q(s, a) = w_a · phi((obs − mu) / sigma)."""

    name = "learned"

    def __init__(
        self,
        weights: np.ndarray,
        mu: np.ndarray,
        sigma: np.ndarray,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.weights = np.asarray(weights, dtype=float)  # (n_actions, n_phi)
        self.mu = np.asarray(mu, dtype=float)
        self.sigma = np.where(np.asarray(sigma, dtype=float) > 0, sigma, 1.0)
        self.meta = meta or {}

    def q_values(self, obs: np.ndarray) -> np.ndarray:
        return self.weights @ phi((np.asarray(obs, dtype=float) - self.mu) / self.sigma)

    def act(self, obs: np.ndarray) -> int:
        return int(np.argmax(self.q_values(obs)))

    def to_artefact(self) -> dict[str, Any]:
        return {
            "kind": "exit_policy",
            "obs_columns": OBS_COLUMNS,
            "actions": list(ACTIONS),
            "mu": self.mu.tolist(),
            "sigma": self.sigma.tolist(),
            "weights": self.weights.tolist(),
            "meta": self.meta,
        }

    @classmethod
    def from_artefact(cls, d: dict[str, Any]) -> LearnedExitPolicy:
        if d.get("obs_columns") != OBS_COLUMNS or d.get("actions") != list(ACTIONS):
            raise ValueError("artefact was trained with a different observation/action space")
        return cls(
            np.asarray(d["weights"]), np.asarray(d["mu"]), np.asarray(d["sigma"]), d.get("meta")
        )

    @classmethod
    def load(cls, path: Path | str) -> LearnedExitPolicy:
        return cls.from_artefact(json.loads(Path(path).read_text()))


def policy_by_name(name: str, seed: int = 0) -> ExitPolicy:
    if name == "ladder":
        return RLadderExitPolicy()
    if name == "hold_only":
        return HoldToTimeStopPolicy()
    if name == "random":
        return RandomExitPolicy(seed)
    if name == "eps_ladder":
        return EpsilonLadderPolicy(seed=seed)
    p = Path(name)
    if p.suffix == ".json" and p.exists():
        return LearnedExitPolicy.load(p)
    raise ValueError(f"unknown exit policy {name!r}")
