"""One WebSocket (``/ws``) pushing a state snapshot every second: control, wallets, positions, marks,
feed health. Slow clients are dropped, never the engine."""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = structlog.get_logger("api.ws")
router = APIRouter()


@router.websocket("/ws")
async def state_stream(ws: WebSocket) -> None:
    await ws.accept()
    eng = getattr(ws.app.state, "engine", None)
    try:
        while True:
            if eng is None:
                payload = {"type": "state", "engine": False}
            else:
                snap = eng.snapshot(brief=True)
                payload = {
                    "type": "state",
                    "engine": True,
                    "ts": snap["ts"],
                    "control": snap["control"],
                    "wallets": snap["wallets"],
                    "positions": snap["positions"],
                    "marks": snap["marks"],
                    "feed": {
                        "connected": snap["feed"]["connected"],
                        "reconnects": snap["feed"]["reconnects"],
                    },
                    "counters": snap["counters"],
                }
            await asyncio.wait_for(ws.send_text(json.dumps(payload, default=str)), timeout=2.0)
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, TimeoutError, RuntimeError):
        return
