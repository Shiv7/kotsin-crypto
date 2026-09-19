"""REST surface: health, engine snapshot, wallets, positions, signals, orders, trades, events, bars,
control (mode / halt)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import __version__
from ..ledger import db as ledgerdb

router = APIRouter()


def _engine(request: Request):
    eng = getattr(request.app.state, "engine", None)
    if eng is None:
        raise HTTPException(503, "engine disabled (KC_ENGINE_ENABLED=false)")
    return eng


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    state = request.app.state
    settings = state.settings
    eng = getattr(state, "engine", None)
    out: dict[str, Any] = {
        "status": "ok",
        "version": __version__,
        "delta_env": settings.delta_env.value,
        "symbols": settings.symbol_list,
        "api_keys_configured": settings.has_api_keys,
        "telegram_configured": state.telegram.enabled,
        "uptime_s": round(time.time() - state.started_at, 1),
        "mode": eng.control.get("mode", "SHADOW") if eng else "SHADOW",
        "engine": bool(eng),
        "bus": state.bus.stats(),
    }
    if eng:
        feed = eng.ws.stats()
        out["feed_connected"] = feed["connected"]
        out["feed_reconnects"] = feed["reconnects"]
        out["open_positions"] = len(eng.positions)
        out["halted"] = bool(eng.control.get("halted"))
        out["wallets"] = {k: round(w.balance, 2) for k, w in eng.wallets.items()}
    return out


@router.get("/system")
async def system(request: Request) -> dict[str, Any]:
    return _engine(request).snapshot()


@router.get("/wallets")
async def wallets_(request: Request) -> dict[str, Any]:
    return _engine(request).snapshot()["wallets"]


@router.get("/positions")
async def positions_(request: Request) -> list[dict[str, Any]]:
    eng = _engine(request)
    return [eng.position_view(p) for p in eng.positions.values()]


@router.get("/signals")
async def signals_(request: Request, limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
    eng = _engine(request)
    return await eng.ledger.recent(ledgerdb.signals, limit=limit, order_col="created_ts")


@router.get("/orders")
async def orders_(request: Request, limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
    return await _engine(request).ledger.recent(ledgerdb.orders, limit=limit, order_col="ts")


@router.get("/trades")
async def trades_(request: Request, limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
    return await _engine(request).ledger.recent(ledgerdb.trades, limit=limit, order_col="closed_ts")


@router.get("/events")
async def events_(request: Request, limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
    return await _engine(request).ledger.recent(ledgerdb.events, limit=limit, order_col="ts")


@router.get("/bars")
async def bars_(
    request: Request, symbol: str, tf: str = "5m", n: int = Query(300, le=3000)
) -> list[dict[str, Any]]:
    eng = _engine(request)
    if symbol not in eng.symbols:
        raise HTTPException(404, f"unknown symbol {symbol}")
    try:
        bars = eng.store.bars(symbol, tf, n)
    except KeyError as exc:
        raise HTTPException(404, f"unknown timeframe {tf}") from exc
    return [asdict(b) for b in bars]


@router.get("/forming")
async def forming_(request: Request, symbol: str, tf: str = "5m") -> dict[str, Any] | None:
    eng = _engine(request)
    if symbol not in eng.symbols:
        raise HTTPException(404, f"unknown symbol {symbol}")
    try:
        bar = eng.forming_bar(symbol, tf)
    except KeyError as exc:
        raise HTTPException(404, f"unknown timeframe {tf}") from exc
    return asdict(bar) if bar else None


@router.get("/micro")
async def micro_(request: Request, symbol: str, levels: int = Query(10, le=15)) -> dict[str, Any]:
    eng = _engine(request)
    if symbol not in eng.symbols:
        raise HTTPException(404, f"unknown symbol {symbol}")
    return eng.micro_view(symbol, levels)


@router.get("/market/perps")
async def market_perps(request: Request, limit: int = Query(80, le=300)) -> dict[str, Any]:
    eng = _engine(request)
    rows = await eng.market.perps()
    return {
        "fetched_ts": eng.market._cache["perps"][0],
        "watched": eng.symbols,
        "rows": rows[:limit],
        "total": len(rows),
    }


@router.get("/options/expiries")
async def options_expiries(request: Request, underlying: str = "BTC") -> list[dict[str, Any]]:
    return await _engine(request).market.option_expiries(underlying.upper())


@router.get("/options/chain")
async def options_chain(request: Request, underlying: str, expiry: str) -> dict[str, Any]:
    try:
        return await _engine(request).market.option_chain(underlying.upper(), expiry)
    except ValueError as exc:
        raise HTTPException(400, f"bad expiry {expiry!r}: {exc}") from exc


class BacktestBody(BaseModel):
    symbols: list[str]
    start: int
    end: int
    tf: str = "5m"
    strategy: str = "CAN2"
    params: dict[str, Any] = {}
    initial_usd: float = 10_000.0
    taker_fee_rate: float = 0.0005
    slippage_bps: dict[str, float] | None = None
    default_slippage_bps: float = 2.0
    apply_funding: bool = True
    limits: dict[str, Any] = {}


def _jobs(request: Request):
    eng = _engine(request)
    if eng.backtests is None:
        raise HTTPException(503, "backtests not ready")
    return eng.backtests


@router.post("/backtest")
async def backtest_submit(request: Request, body: BacktestBody) -> dict[str, Any]:
    try:
        return _jobs(request).submit(body.model_dump())
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/backtest")
async def backtest_list(request: Request) -> list[dict[str, Any]]:
    return _jobs(request).list()


@router.get("/backtest/{job_id}")
async def backtest_get(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return _jobs(request).status(job_id)
    except KeyError as exc:
        raise HTTPException(404, "unknown backtest") from exc


@router.get("/backtest/{job_id}/bars")
async def backtest_bars(
    request: Request, job_id: str, symbol: str, tf: str = "5m", n: int = Query(5000, le=20000)
) -> list[dict[str, Any]]:
    try:
        return await _jobs(request).bars(job_id, symbol, tf, n)
    except KeyError as exc:
        raise HTTPException(404, f"unknown {exc}") from exc


@router.get("/committee/status")
async def committee_status(request: Request) -> dict[str, Any]:
    return _engine(request).committee.status()


@router.get("/committee/latest")
async def committee_latest(request: Request) -> dict[str, Any]:
    return _engine(request).committee.latest


@router.get("/committee/log")
async def committee_log(request: Request, limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
    entries = _engine(request).committee.log.entries
    return [{k: v for k, v in e.items() if k not in ("run", "pack")} for e in entries[-limit:]][
        ::-1
    ]


@router.get("/committee/entry/{entry_id}")
async def committee_entry(request: Request, entry_id: str) -> dict[str, Any]:
    e = next((x for x in _engine(request).committee.log.entries if x["id"] == entry_id), None)
    if e is None:
        raise HTTPException(404, "unknown entry")
    return e


class CommitteeRunBody(BaseModel):
    symbol: str


@router.post("/committee/run")
async def committee_run(request: Request, body: CommitteeRunBody) -> dict[str, Any]:
    eng = _engine(request)
    if body.symbol not in eng.symbols:
        raise HTTPException(404, f"unknown symbol {body.symbol}")
    try:
        entry = await eng.committee.run_symbol(body.symbol)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {k: v for k, v in entry.items() if k != "pack"}


@router.get("/committee/labels")
async def committee_labels(
    request: Request, symbol: str, n: int = Query(600, le=3000)
) -> dict[str, Any]:
    from ..committee.labels import label_series

    eng = _engine(request)
    if symbol not in eng.symbols:
        raise HTTPException(404, f"unknown symbol {symbol}")
    bars = eng.store.bars(symbol, "5m", n)
    z, labels = label_series([b.close for b in bars])
    counts: dict[str, int] = {}
    for lab in labels:
        if lab is not None:
            counts[lab.value] = counts.get(lab.value, 0) + 1
    return {
        "symbol": symbol,
        "bars": len(bars),
        "labelled": sum(counts.values()),
        "distribution": counts,
        "series": [
            {
                "ts": b.ts,
                "close": b.close,
                "z": None if not (z[i] == z[i]) else round(float(z[i]), 3),
                "label": labels[i].value if labels[i] else None,
            }
            for i, b in enumerate(bars)
        ],
    }


class ProbeBody(BaseModel):
    symbol: str
    side: str = "LONG"
    confirm: bool = False


@router.post("/control/probe")
async def probe_entry(request: Request, body: ProbeBody) -> dict[str, Any]:
    """Plumbing test: push a synthetic 1-contract-sized signal through the PRODUCTION entry path
    (risk → gateway → paper/live executor). Requires confirm=true. In a live mode this places a real
    order under the LIVE_CAPPED caps."""
    from decimal import Decimal

    from ..strategy.base import Side, Signal
    from ..strategy.keys import StrategyKey

    eng = _engine(request)
    if not body.confirm:
        raise HTTPException(400, "set confirm=true to run a probe")
    if body.symbol not in eng.symbols:
        raise HTTPException(404, f"unknown symbol {body.symbol}")
    side = Side.LONG if body.side.upper() == "LONG" else Side.SHORT
    mark = eng.marks.get(body.symbol) or eng.last_price.get(body.symbol)
    if not mark:
        raise HTTPException(409, "no mark price yet")
    bars = eng.store.bars(body.symbol, "5m", 20)
    if not bars:
        raise HTTPException(409, "no bars yet")
    trs = [
        max(b.high - b.low, abs(b.high - bars[i - 1].close), abs(b.low - bars[i - 1].close))
        for i, b in enumerate(bars)
        if i > 0
    ]
    atr = sum(trs[-14:]) / max(1, len(trs[-14:])) if trs else mark * 0.003
    dist = max(1.5 * atr, mark * 0.003)
    stop = mark - dist if side is Side.LONG else mark + dist
    now = time.time()
    sig = Signal(
        strategy=StrategyKey.CAN2,
        symbol=body.symbol,
        side=side,
        ts=int(now),
        entry=Decimal(str(mark)),
        stop=Decimal(str(round(stop, 8))),
        confidence=1.0,
        reason="PROBE (manual plumbing test)",
        evidence={"atr": atr, "surge": 0.0},
    )
    before = set(eng.positions)
    eng._on_signal(sig, bars[-1], now)
    return {
        "signal_id": sig.signal_id,
        "mark": mark,
        "stop": stop,
        "mode": eng.control.get("mode"),
        "positions_before": sorted(before),
        "recent_signal": eng.recent_signals[0] if eng.recent_signals else None,
    }


class ProbeExitBody(BaseModel):
    position_id: str
    confirm: bool = False


@router.post("/control/probe_exit")
async def probe_exit(request: Request, body: ProbeExitBody) -> dict[str, Any]:
    """Close one position through the PRODUCTION exit path (manual reason)."""
    from ..domain import ExitDecision, ExitReason

    eng = _engine(request)
    if not body.confirm:
        raise HTTPException(400, "set confirm=true to run a probe exit")
    pos = eng.positions.get(body.position_id)
    if pos is None:
        raise HTTPException(404, "unknown or closed position")
    mark = eng.marks.get(pos.symbol) or eng.last_price.get(pos.symbol) or pos.entry
    eng._close_position(
        pos, ExitDecision(pos.id, ExitReason.MANUAL, mark, "probe exit"), time.time()
    )
    return {"position_id": pos.id, "status": pos.status, "mode": eng.control.get("mode")}


def _rl_dir(request: Request) -> Path:
    return Path(request.app.state.settings.data_dir) / "rl"


@router.get("/rl/runs")
async def rl_runs(request: Request) -> list[dict[str, Any]]:
    """Research artefacts (exit-policy / bandit experiments) under data/rl, newest first."""
    out = []
    for path in sorted(_rl_dir(request).glob("*.json")):
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        out.append(
            {
                "name": path.stem,
                "kind": d.get("kind"),
                "created_ts": d.get("created_ts"),
                "summary": d.get("summary"),
                "config": {
                    k: v
                    for k, v in (d.get("config") or {}).items()
                    if k not in ("arms", "obs_columns", "actions")
                },
            }
        )
    out.sort(key=lambda r: float(r["created_ts"] or 0), reverse=True)
    return out


@router.get("/rl/runs/{name}")
async def rl_run(request: Request, name: str) -> dict[str, Any]:
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    path = _rl_dir(request) / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, "unknown run")
    d = json.loads(path.read_text())
    d.pop("policy", None)  # weights are large and not useful in the UI
    return d


class ModeBody(BaseModel):
    mode: str
    arm_hours: float | None = None  # required for LIVE_CAPPED / LIVE (0.5–24)


class HaltBody(BaseModel):
    halted: bool
    reason: str = ""


@router.get("/control")
async def control_(request: Request) -> dict[str, Any]:
    eng = _engine(request)
    return {**eng.control, "armed": eng.live_armed(), "live_available": eng.live is not None}


@router.post("/control/mode")
async def set_mode(request: Request, body: ModeBody) -> dict[str, Any]:
    try:
        return await _engine(request).set_mode(body.mode.upper(), arm_hours=body.arm_hours)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/control/halt")
async def set_halt(request: Request, body: HaltBody) -> dict[str, Any]:
    return await _engine(request).set_halt(body.halted, body.reason)


@router.post("/control/kill")
async def kill(request: Request) -> dict[str, Any]:
    """Halt entries, cancel every venue order and close every venue position at market."""
    return await _engine(request).kill("manual kill (API)")


@router.get("/control/live")
async def live_status(request: Request) -> dict[str, Any]:
    eng = _engine(request)
    return {
        "mode": eng.control.get("mode"),
        "armed": eng.live_armed(),
        "armed_until": eng.control.get("armed_until"),
        "live": eng.live.status() if eng.live else None,
        "private_ws": eng.live_ws.stats() if eng.live_ws else None,
        "reconcile": eng.reconciler.status(),
        "caps": eng.gateway.stats().get("caps"),
        "venue_positions": eng.venue_positions,
    }


@router.post("/control/reconcile")
async def reconcile_now(request: Request) -> dict[str, Any]:
    eng = _engine(request)
    if eng.live is None:
        raise HTTPException(409, "no API keys configured")
    return (await eng.reconciler.reconcile_once()).to_json()
