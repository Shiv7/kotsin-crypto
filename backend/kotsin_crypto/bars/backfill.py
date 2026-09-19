"""Warm start: seed the 1m store from REST candles so indicators have history before the first live
bar. Backfilled bars are ``source="rest"`` with no taker split (``buy_volume``/``sell_volume`` = 0)."""

from __future__ import annotations

import time
from typing import Any

import structlog

from ..venue.delta.rest import DeltaRest
from .unified import UnifiedBar

log = structlog.get_logger("backfill")

CHUNK_MIN = 3000  # < the ~4000-row cap observed per request


async def fetch_1m(
    rest: DeltaRest, symbol: str, *, hours: float, now: float | None = None
) -> list[UnifiedBar]:
    end = int(now if now is not None else time.time())
    end -= end % 60  # exclude the still-forming minute
    start = end - int(hours * 3600)
    rows: dict[int, dict[str, Any]] = {}
    cur = start
    while cur < end:
        chunk_end = min(end, cur + CHUNK_MIN * 60)
        for c in await rest.candles(symbol, "1m", cur, chunk_end):
            rows[int(c["time"])] = c
        cur = chunk_end
    bars = [
        UnifiedBar(
            symbol=symbol,
            tf="1m",
            ts=ts,
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
            volume=float(c.get("volume") or 0.0),
            has_trades=float(c.get("volume") or 0.0) > 0,
            source="rest",
        )
        for ts, c in sorted(rows.items())
        if ts < end
    ]
    log.info("backfill", symbol=symbol, bars=len(bars), start=start, end=end)
    return bars
