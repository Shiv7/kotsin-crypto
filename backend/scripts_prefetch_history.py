"""Prefetch 1m history into the local Parquet cache (public REST, no key)."""

import asyncio
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from kotsin_crypto.config import DeltaEnv, Settings
from kotsin_crypto.research.history import HistoryStore
from kotsin_crypto.venue.delta.rest import DeltaRest


async def main() -> None:
    symbols = sys.argv[1].split(",") if len(sys.argv) > 1 else ["BTCUSD", "ETHUSD"]
    d0 = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date(2025, 6, 1)
    d1 = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else date(2026, 9, 19)
    rest = DeltaRest(Settings(_env_file=None, delta_env=DeltaEnv.MAINNET, engine_enabled=False))
    hs = HistoryStore(rest, Path("data/history"))
    t0 = time.time()
    n = 0
    for sym in symbols:
        d = d0
        while d <= d1:
            rows = await hs.fetch_day(sym, d)
            n += 1
            if n % 20 == 0:
                print(
                    f"{sym} {d} rows={len(rows)} budget_used={rest.budget.used()} elapsed={time.time() - t0:.0f}s",
                    flush=True,
                )
            d += timedelta(days=1)
    print(
        "DONE days:",
        n,
        "seconds:",
        round(time.time() - t0),
        {s: hs.cached_days(s) for s in symbols},
        flush=True,
    )
    await rest.aclose()


asyncio.run(main())
