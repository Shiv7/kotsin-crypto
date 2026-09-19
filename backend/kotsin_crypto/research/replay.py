"""Rebuild 1m UnifiedBars WITH microstructure from the JSONL archive, through the engine's own
builders (TradeBarBuilder, MicroBuilder, merge_1m) fed by the same parser (feed/parse.py). Messages
are replayed in receive order across channels (k-way merge on the archived ``t``), and the engine's
1 s clock is emulated by flushing the trade-bar builder at each message's receive time, so a bar
closes exactly as it would have live. Deterministic: same archive → byte-identical output.

CLI:  python -m kotsin_crypto.research.replay --archive backend/data/archive --symbol BTCUSD --out bars.parquet
"""

from __future__ import annotations

import argparse
import heapq
import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ..bars.micro import MicroBuilder
from ..bars.trade_bar import TradeBarBuilder
from ..bars.unified import UnifiedBar, merge_1m
from ..feed.parse import FundingEvt, L1Evt, L2Evt, MarkEvt, TickerEvt, TradeEvt, parse

CHANNELS = ("trades", "ob_l1", "ob_l2", "mark_price", "funding_rate", "ticker")


def _channel_stream(archive: Path, channel: str) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """(recv_ts_us, seq, msg) for one channel; files in (day, hour) order = arrival order."""
    seq = 0
    for path in sorted((archive / channel).glob("*/*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                yield int(d["t"]), seq, d["m"]
                seq += 1


def iter_archive(
    archive: Path, channels: tuple[str, ...] = CHANNELS
) -> Iterator[tuple[int, dict[str, Any]]]:
    streams = [_channel_stream(archive, ch) for ch in channels if (archive / ch).is_dir()]
    for t, _seq, msg in heapq.merge(*streams, key=lambda x: (x[0], x[1])):
        yield t, msg


def estimate_daily_volume(archive: Path, symbol: str) -> float | None:
    """Contracts per 24h extrapolated from the archived trades (for VPIN bucket sizing when the
    ticker's 24h size is not available offline)."""
    total = 0.0
    first = last = None
    for t, msg in iter_archive(archive, ("trades",)):
        if msg.get("type") != "trades" or msg.get("sy") != symbol:
            continue
        total += float(msg.get("s") or 0)
        first = t if first is None else first
        last = t
    if first is None or last is None or last <= first:
        return None
    span_s = (last - first) / 1e6
    return total * 86_400 / span_s if span_s > 60 else None


def replay(
    archive: Path,
    symbol: str,
    *,
    daily_volume: float | None = None,
    channels: tuple[str, ...] = CHANNELS,
) -> list[UnifiedBar]:
    if daily_volume is None:
        daily_volume = estimate_daily_volume(archive, symbol)
    tb = TradeBarBuilder(symbol)
    mb = MicroBuilder(symbol, daily_volume=daily_volume)
    marks: dict[str, float] = {}
    funding: dict[str, float] = {}
    oi: dict[str, float] = {}
    out: list[UnifiedBar] = []
    first_ts: int | None = None

    def close(bars: list[Any]) -> None:
        for trade_bar in bars:
            micro = mb.close(trade_bar.ts)
            partial = (
                trade_bar.trade_count > 0 and first_ts is not None and trade_bar.ts < first_ts / 1e6
            )
            out.append(
                merge_1m(
                    trade_bar,
                    micro,
                    oi=oi.get(symbol),
                    funding_rate=funding.get(symbol),
                    mark_close=marks.get(symbol),
                    source="partial" if partial else "live",
                )
            )

    for t, msg in iter_archive(archive, channels):
        if first_ts is None:
            first_ts = t
        evt = parse(msg)
        if evt is None or getattr(evt, "symbol", symbol) != symbol:
            close(tb.flush(t / 1e6))
            continue
        if isinstance(evt, TradeEvt):
            mb.on_trade(evt.pub_ts_us or evt.ts_us, evt.price, evt.size, evt.taker_buy)
            close(tb.on_trade(evt.ts_us, evt.price, evt.size, evt.taker_buy))
        elif isinstance(evt, L1Evt):
            mb.on_quote(evt.ts_us, evt.bid, evt.bid_size, evt.ask, evt.ask_size)
        elif isinstance(evt, L2Evt):
            mb.on_l2(evt.ts_us, evt.bids, evt.asks)
        elif isinstance(evt, MarkEvt):
            marks[evt.symbol] = evt.price
        elif isinstance(evt, FundingEvt):
            funding[evt.symbol] = evt.rate_pct
        elif isinstance(evt, TickerEvt):
            if evt.oi_contracts is not None:
                oi[evt.symbol] = evt.oi_contracts
        close(tb.flush(t / 1e6))
    return out


def bars_to_table(bars: list[UnifiedBar]) -> pa.Table:
    rows = [asdict(b) for b in bars]
    return pa.Table.from_pylist(rows) if rows else pa.table({"ts": pa.array([], pa.int64())})


def table_to_bars(table: pa.Table) -> list[UnifiedBar]:
    return [UnifiedBar(**r) for r in table.to_pylist()]


def write_bars(bars: list[UnifiedBar], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(bars_to_table(bars), out, compression="zstd")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Rebuild 1m bars with microstructure from the JSONL archive."
    )
    ap.add_argument("--archive", required=True, type=Path)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument(
        "--daily-volume",
        type=float,
        default=None,
        help="24h contract volume for VPIN buckets (estimated from the archive when omitted)",
    )
    a = ap.parse_args(argv)
    bars = replay(a.archive, a.symbol, daily_volume=a.daily_volume)
    write_bars(bars, a.out)
    live = sum(1 for b in bars if b.source == "live")
    micro = sum(1 for b in bars if b.has_micro)
    print(
        f"{a.symbol}: {len(bars)} bars ({live} live, {len(bars) - live} partial), {micro} with microstructure → {a.out}"
    )


if __name__ == "__main__":
    main()
