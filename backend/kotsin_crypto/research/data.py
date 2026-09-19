"""Datasets for research: load 1m bars from the Parquet history cache (or the archive replay),
resample to 5m, and export point-in-time feature frames.

CLI:  python -m kotsin_crypto.research.data export --symbols BTCUSD,ETHUSD --start 2025-06-01 --end 2026-09-19 --out data/rl/features
      python -m kotsin_crypto.research.data export --symbols BTCUSD --replay bars.parquet --out data/rl/features
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..bars.unified import UnifiedBar
from ..config import DeltaEnv, Settings
from ..venue.delta.rest import DeltaRest
from .features import FEATURE_COLUMNS, TARGET_COLUMNS, build_frame, resample_5m
from .history import HistoryStore, day_start, rows_to_bars
from .replay import table_to_bars


def parse_day(s: str) -> int:
    return day_start(date.fromisoformat(s))


def load_cached_1m(symbol: str, start: int, end: int, root: Path) -> list[UnifiedBar]:
    """1m bars from the local cache only (no network); missing days are skipped."""
    d0 = datetime.fromtimestamp(start, tz=UTC).date()
    d1 = datetime.fromtimestamp(max(start, end - 1), tz=UTC).date()
    rows: list[dict[str, Any]] = []
    d = d0
    while d <= d1:
        p = root / "1m" / symbol / f"{d.isoformat()}.parquet"
        if p.exists():
            rows.extend(r for r in pq.read_table(p).to_pylist() if start <= r["ts"] < end)
        d += timedelta(days=1)
    rows.sort(key=lambda r: r["ts"])
    return rows_to_bars(symbol, rows)


async def load_1m(
    symbol: str, start: int, end: int, root: Path, rest: DeltaRest | None = None
) -> list[UnifiedBar]:
    """1m bars via the cache, fetching missing days from REST when a client is given."""
    if rest is None:
        return load_cached_1m(symbol, start, end, root)
    hs = HistoryStore(rest, root)
    days = await hs.iter_days(symbol, start, end)
    return rows_to_bars(symbol, [r for day in days for r in day])


def frame_to_table(symbol: str, frame: dict[str, np.ndarray]) -> pa.Table:
    cols = {
        "symbol": pa.array([symbol] * len(frame["ts"])),
        "ts": pa.array(frame["ts"].astype(np.int64)),
        "close": pa.array(frame["close"]),
    }
    for c in FEATURE_COLUMNS + TARGET_COLUMNS:
        cols[c] = pa.array(np.asarray(frame[c], dtype=float))
    return pa.table(cols)


def export_frame(symbol: str, bars_1m: list[UnifiedBar], out_dir: Path) -> tuple[Path, int]:
    bars5 = resample_5m(symbol, bars_1m)
    frame = build_frame(symbol, bars5)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}_5m.parquet"
    pq.write_table(frame_to_table(symbol, frame), path, compression="zstd")
    return path, len(bars5)


def load_frame(path: Path) -> dict[str, np.ndarray]:
    t = pq.read_table(path)
    return {name: t.column(name).to_numpy(zero_copy_only=False) for name in t.column_names}


async def _export_cli(a: argparse.Namespace) -> None:
    out = Path(a.out)
    if a.replay:
        bars = table_to_bars(pq.read_table(a.replay))
        symbol = a.symbols.split(",")[0]
        path, n = export_frame(symbol, bars, out)
        print(f"{symbol}: {n} 5m bars from replay → {path}")
        return
    rest = (
        None
        if a.offline
        else DeltaRest(Settings(_env_file=None, delta_env=DeltaEnv.MAINNET, engine_enabled=False))
    )
    try:
        for symbol in a.symbols.split(","):
            bars = await load_1m(
                symbol, parse_day(a.start), parse_day(a.end) + 86_400, Path(a.history), rest
            )
            path, n = export_frame(symbol, bars, out)
            print(f"{symbol}: {len(bars)} 1m bars → {n} 5m rows → {path}")
    finally:
        if rest is not None:
            await rest.aclose()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Research datasets")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="export 5m feature frames to Parquet")
    ex.add_argument("--symbols", required=True)
    ex.add_argument("--start", default="2025-06-01")
    ex.add_argument("--end", default=date.today().isoformat())
    ex.add_argument("--out", default="data/rl/features")
    ex.add_argument("--history", default="data/history")
    ex.add_argument(
        "--replay", default=None, help="Parquet from research.replay instead of REST history"
    )
    ex.add_argument("--offline", action="store_true", help="cache only, never call REST")
    a = ap.parse_args(argv)
    if a.cmd == "export":
        asyncio.run(_export_cli(a))


if __name__ == "__main__":
    main()
