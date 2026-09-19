"""Historical 1m candles from Delta's REST, cached on disk one Parquet file per (symbol, UTC day) so a
backtest over the same range never refetches. Complete days only are cached; today is always live."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import structlog

from ..bars.unified import UnifiedBar
from ..venue.delta.rest import DeltaRest

log = structlog.get_logger("history")

DAY_S = 86_400
SCHEMA = pa.schema(
    [
        ("ts", pa.int64()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
    ]
)


def day_of(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=UTC).date()


def day_start(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


class HistoryStore:
    def __init__(self, rest: DeltaRest, root: Path) -> None:
        self.rest = rest
        self.root = root

    def _path(self, symbol: str, d: date) -> Path:
        return self.root / "1m" / symbol / f"{d.isoformat()}.parquet"

    async def fetch_day(self, symbol: str, d: date) -> list[dict[str, Any]]:
        """Rows for one UTC day, from cache when complete, else from REST (two requests at most)."""
        path = self._path(symbol, d)
        if path.exists():
            return pq.read_table(path).to_pylist()
        start, end = day_start(d), day_start(d) + DAY_S
        now = int(time.time())
        rows: dict[int, dict[str, Any]] = {}
        for s in range(start, min(end, now), DAY_S // 2):
            e = min(s + DAY_S // 2, end, now)
            for c in await self.rest.candles(symbol, "1m", s, e):
                t = int(c["time"])
                if start <= t < end:
                    rows[t] = {
                        "ts": t,
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low": float(c["low"]),
                        "close": float(c["close"]),
                        "volume": float(c.get("volume") or 0.0),
                    }
        out = [rows[t] for t in sorted(rows)]
        if end <= now - 120 and out:  # complete day → cache
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(out, schema=SCHEMA), path)
        return out

    def cached_days(self, symbol: str) -> int:
        d = self.root / "1m" / symbol
        return len(list(d.glob("*.parquet"))) if d.is_dir() else 0

    async def iter_days(
        self, symbol: str, start: int, end: int, progress: Callable[[int, int], None] | None = None
    ) -> list[list[dict[str, Any]]]:
        d0, d1 = day_of(start), day_of(max(start, end - 1))
        days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
        out = []
        for i, d in enumerate(days):
            rows = await self.fetch_day(symbol, d)
            out.append([r for r in rows if start <= r["ts"] < end])
            if progress:
                progress(i + 1, len(days))
        return out

    async def funding_series(self, symbol: str, start: int, end: int) -> list[tuple[int, float]]:
        """(ts, rate_pct) points from the FUNDING: series at 1h resolution; empty if unavailable."""
        pts: dict[int, float] = {}
        try:
            for s in range(start - 8 * 3600, end, 3000 * 3600):
                e = min(s + 3000 * 3600, end)
                for c in await self.rest.candles(f"FUNDING:{symbol}", "1h", s, e):
                    pts[int(c["time"])] = float(c["close"])
        except Exception as exc:  # funding history is optional
            log.warning("funding_history_unavailable", symbol=symbol, error=str(exc))
        return sorted(pts.items())


def rows_to_bars(symbol: str, rows: list[dict[str, Any]]) -> list[UnifiedBar]:
    return [
        UnifiedBar(
            symbol=symbol,
            tf="1m",
            ts=int(r["ts"]),
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            has_trades=r["volume"] > 0,
            source="rest",
        )
        for r in rows
    ]
