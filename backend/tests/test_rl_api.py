from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from kotsin_crypto.api.routes import rl_run, rl_runs


def _request(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(data_dir=str(data_dir))))
    )


def test_rl_runs_lists_artefacts_newest_first_and_hides_weights(tmp_path: Path) -> None:
    rl = tmp_path / "rl"
    rl.mkdir()
    (rl / "exit_policy_20260101_0000.json").write_text(
        json.dumps(
            {
                "kind": "exit_policy",
                "created_ts": 1,
                "summary": {"episodes": 3},
                "config": {"seed": 0, "actions": ["hold"], "obs_columns": ["r_now"]},
                "policy": {"weights": [[1.0]]},
            }
        )
    )
    (rl / "bandit_20260202_0000.json").write_text(
        json.dumps({"kind": "bandit", "created_ts": 2, "summary": {}, "config": {"arms": [1, 2]}})
    )
    (rl / "broken.json").write_text("{not json")
    req = _request(tmp_path)
    runs = asyncio.run(rl_runs(req))  # type: ignore[arg-type]
    assert [r["name"] for r in runs] == ["bandit_20260202_0000", "exit_policy_20260101_0000"]
    assert runs[1]["config"] == {"seed": 0} and runs[0]["config"] == {}
    one = asyncio.run(rl_run(req, "exit_policy_20260101_0000"))  # type: ignore[arg-type]
    assert one["kind"] == "exit_policy" and "policy" not in one
    with pytest.raises(HTTPException):
        asyncio.run(rl_run(req, "../secrets"))  # type: ignore[arg-type]
    with pytest.raises(HTTPException):
        asyncio.run(rl_run(req, "missing"))  # type: ignore[arg-type]
