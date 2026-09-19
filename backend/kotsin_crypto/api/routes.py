"""REST surface: health, engine snapshot, wallets, positions, signals, orders, trades, events, bars,
control (mode / halt)."""

from __future__ import annotations

import time
from dataclasses import asdict
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


class ModeBody(BaseModel):
    mode: str


class HaltBody(BaseModel):
    halted: bool
    reason: str = ""


@router.get("/control")
async def control_(request: Request) -> dict[str, Any]:
    return _engine(request).control


@router.post("/control/mode")
async def set_mode(request: Request, body: ModeBody) -> dict[str, Any]:
    try:
        return await _engine(request).set_mode(body.mode.upper())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/control/halt")
async def set_halt(request: Request, body: HaltBody) -> dict[str, Any]:
    return await _engine(request).set_halt(body.halted, body.reason)
