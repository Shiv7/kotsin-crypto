"""Append-only JSONL archive of raw feed events (ADR-0003, refined: JSONL rather than Parquet for the
hot path because plain appends are crash-safe and DuckDB reads them directly; a research job compacts
to Parquet).

Layout: ``<root>/<channel>/<YYYY-MM-DD>/<HH>.jsonl``, one line per event ``{"t": <recv µs>, "m": <payload>}``.
The event loop only appends to an in-memory buffer; a background task swaps the buffer and writes it on
a thread every few seconds, so a slow disk never stalls the feed.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import structlog

log = structlog.get_logger("archive")


class Archive:
    def __init__(self, root: Path, *, flush_every_s: float = 5.0) -> None:
        self.root = root
        self.flush_every_s = flush_every_s
        self._buf: dict[str, list[str]] = defaultdict(list)
        self._files: dict[tuple[str, str], IO[str]] = {}
        self.rows: dict[str, int] = defaultdict(int)
        self.bytes: dict[str, int] = defaultdict(int)
        self.last_flush_ts = 0.0
        self.flushes = 0
        self.errors = 0

    def append(self, channel: str, payload_json: str, recv_ts_us: int) -> None:
        self._buf[channel].append(f'{{"t":{recv_ts_us},"m":{payload_json}}}\n')

    def _take(self) -> dict[str, list[str]]:
        batch = {ch: lines for ch, lines in self._buf.items() if lines}
        self._buf = defaultdict(list)
        return batch

    def _handle(self, channel: str, ts_s: float) -> IO[str]:
        hourkey = datetime.fromtimestamp(ts_s, tz=UTC).strftime("%Y-%m-%d/%H")
        key = (channel, hourkey)
        fh = self._files.get(key)
        if fh is None:
            for stale in [k for k in self._files if k[0] == channel and k != key]:
                self._files.pop(stale).close()
            path = self.root / channel / f"{hourkey}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = self._files[key] = open(path, "a", encoding="utf-8")
        return fh

    def _write(self, batch: dict[str, list[str]]) -> int:
        now = time.time()
        n = 0
        for channel, lines in batch.items():
            try:
                fh = self._handle(channel, now)
                data = "".join(lines)
                fh.write(data)
                fh.flush()
                self.rows[channel] += len(lines)
                self.bytes[channel] += len(data)
                n += len(lines)
            except OSError as exc:
                self.errors += 1
                log.error("archive_write_failed", channel=channel, error=str(exc))
        self.last_flush_ts = now
        self.flushes += 1
        return n

    def flush(self) -> int:
        """Synchronous flush (tests, shutdown)."""
        return self._write(self._take())

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.flush_every_s)
            except TimeoutError:
                pass
            batch = self._take()
            if batch:
                await asyncio.to_thread(self._write, batch)
        self.flush()
        self.close()

    def close(self) -> None:
        for fh in self._files.values():
            fh.close()
        self._files.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "rows": dict(self.rows),
            "bytes": dict(self.bytes),
            "flushes": self.flushes,
            "errors": self.errors,
            "buffered": sum(len(v) for v in self._buf.values()),
            "last_flush_age_s": round(time.time() - self.last_flush_ts, 1)
            if self.last_flush_ts
            else None,
        }
