"""Backtest jobs: fetch history (async, with progress), run the replay on a thread, persist the result
as JSON under data/backtests/<id>.json, and serve bars for the result chart."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import structlog

from ..bars.unified import TF_SECONDS, BarStore
from ..domain import new_id
from ..venue.delta.catalogue import Catalogue
from ..venue.delta.rest import DeltaRest
from .backtest import BacktestRunner, ProductSpec, config_from_dict, config_to_dict
from .history import HistoryStore, rows_to_bars

log = structlog.get_logger("backtest.jobs")

MAX_DAYS = 400


class BacktestJobs:
    def __init__(
        self, rest: DeltaRest, history_root: Path, out_root: Path, catalogue: Catalogue
    ) -> None:
        self.history = HistoryStore(rest, history_root)
        self.out_root = out_root
        self.catalogue = catalogue
        self.jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.out_root.mkdir(parents=True, exist_ok=True)

    def submit(self, cfg_dict: dict[str, Any]) -> dict[str, Any]:
        cfg = config_from_dict(cfg_dict)
        if not cfg.symbols:
            raise ValueError("no symbols")
        if cfg.end <= cfg.start:
            raise ValueError("end must be after start")
        if (cfg.end - cfg.start) / 86_400 > MAX_DAYS:
            raise ValueError(f"range longer than {MAX_DAYS} days")
        for s in cfg.symbols:
            if s not in self.catalogue:
                raise ValueError(f"unknown symbol {s}")
        job_id = new_id("bt")
        job = {
            "id": job_id,
            "status": "queued",
            "cfg": config_to_dict(cfg),
            "submitted_ts": time.time(),
            "progress": {},
            "error": None,
            "result": None,
        }
        self.jobs[job_id] = job
        self._tasks[job_id] = asyncio.create_task(self._run(job_id))
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            saved = self._load(job_id)
            if saved is None:
                raise KeyError(job_id)
            return {**saved, "status": "done"}
        out = {k: v for k, v in job.items() if k != "result"}
        if job["result"] is not None:
            out["result"] = job["result"]
        return out

    def list(self) -> list[dict[str, Any]]:
        rows = []
        for job in self.jobs.values():
            rows.append(
                {
                    "id": job["id"],
                    "status": job["status"],
                    "cfg": job["cfg"],
                    "submitted_ts": job["submitted_ts"],
                    "stats": (job["result"] or {}).get("stats"),
                }
            )
        seen = {r["id"] for r in rows}
        for path in sorted(self.out_root.glob("bt-*.json"), reverse=True):
            if path.stem in seen:
                continue
            try:
                d = json.loads(path.read_text())
                rows.append(
                    {
                        "id": d["id"],
                        "status": "done",
                        "cfg": d["cfg"],
                        "submitted_ts": d.get("submitted_ts"),
                        "stats": (d.get("result") or {}).get("stats"),
                    }
                )
            except (OSError, ValueError):
                continue
        rows.sort(key=lambda r: -(r.get("submitted_ts") or 0))
        return rows

    def _load(self, job_id: str) -> dict[str, Any] | None:
        path = self.out_root / f"{job_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    async def _run(self, job_id: str) -> None:
        job = self.jobs[job_id]
        cfg = config_from_dict(job["cfg"])
        try:
            job["status"] = "fetching"
            bars_by_symbol = {}
            funding = {}
            for sym in cfg.symbols:

                def progress(done: int, total: int, sym: str = sym) -> None:
                    job["progress"][sym] = {"days_done": done, "days_total": total}

                days = await self.history.iter_days(sym, cfg.start, cfg.end, progress)
                bars_by_symbol[sym] = rows_to_bars(sym, [r for day in days for r in day])
                funding[sym] = (
                    await self.history.funding_series(sym, cfg.start, cfg.end)
                    if cfg.apply_funding
                    else []
                )
            products = {
                s: ProductSpec(
                    float(self.catalogue.by_symbol(s).contract_value),
                    float(self.catalogue.by_symbol(s).maintenance_margin_pct or 0.5),
                )
                for s in cfg.symbols
            }
            job["status"] = "running"
            job["started_ts"] = time.time()
            runner = BacktestRunner(cfg, products, funding)
            result = await asyncio.to_thread(runner.run, bars_by_symbol)
            result["bars_by_symbol"] = {s: len(b) for s, b in bars_by_symbol.items()}
            job["result"] = result
            job["status"] = "done"
            job["finished_ts"] = time.time()
            (self.out_root / f"{job_id}.json").write_text(
                json.dumps({k: v for k, v in job.items()}, default=str)
            )
            log.info(
                "backtest_done",
                id=job_id,
                trades=result["stats"]["trades"],
                net=round(result["stats"]["net"], 2),
                seconds=round(job["finished_ts"] - job["started_ts"], 1),
            )
        except Exception as exc:
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"
            log.exception("backtest_failed", id=job_id)

    async def bars(self, job_id: str, symbol: str, tf: str, n: int = 5000) -> list[dict[str, Any]]:
        """Price bars for the job's range at ``tf`` (rebuilt from the cached 1m history)."""
        job = self.jobs.get(job_id) or self._load(job_id)
        if job is None:
            raise KeyError(job_id)
        cfg = config_from_dict(job["cfg"])
        if symbol not in cfg.symbols:
            raise KeyError(symbol)
        if tf not in TF_SECONDS:
            raise KeyError(tf)
        days = await self.history.iter_days(symbol, cfg.start, cfg.end)
        store = BarStore([symbol], tfs=("1m", tf) if tf != "1m" else ("1m",), maxlen=max(n, 10))
        for b in rows_to_bars(symbol, [r for day in days for r in day]):
            store.add_1m(b)
        out = store.bars(symbol, tf, n)
        forming = store.forming(symbol, tf, None) if tf != "1m" else None
        if forming is not None:
            out.append(forming)
        from dataclasses import asdict

        return [asdict(b) for b in out]
