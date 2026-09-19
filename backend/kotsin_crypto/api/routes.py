"""REST surface. Step 1: health. Step 7: wallets, positions, signals, trades, risk, control."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

from .. import __version__

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    state = request.app.state
    settings = state.settings
    return {
        "status": "ok",
        "version": __version__,
        "delta_env": settings.delta_env.value,
        "symbols": settings.symbol_list,
        "api_keys_configured": settings.has_api_keys,
        "telegram_configured": state.telegram.enabled,
        "uptime_s": round(time.time() - state.started_at, 1),
        # Mode comes from the control table once ledger/ exists (step 6). Until then: SHADOW.
        "mode": "SHADOW",
        "bus": state.bus.stats(),
    }
