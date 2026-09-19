# ADR-0003 — SQLite for the ledger, Parquet for market data

**Status:** accepted · 2026-09-20

## Context
Orders, fills, positions and the control row need ACID and a single writer. Market data needs append
speed and columnar reads for research. Neither needs a server on a personal box.

## Decision
- `data/kotsin_crypto.db`: SQLite in WAL mode via SQLAlchemy async — orders, fills, positions, trades,
  wallet snapshots, signals (incl. rejections), control.
- `data/archive/<channel>/<yyyy-mm-dd>/<HH>.jsonl`: every raw WS event as `{"t":recv_µs,"m":payload}`,
  flushed every 5 s. **Refined 2026-09-20:** JSONL on the hot path (plain appends are crash-safe; a
  Parquet file without its footer is unreadable), compacted to Parquet by a research job. ~0.6 MB/min for
  three symbols with `ob_l1` + `ob_l2` (≈ 0.8 GB/day).
- DuckDB queries the archive directly for backtests and reports.
- JSONL export of closed trades (same shape as the NSE ledger, plus funding/leverage/mark fields) for
  durability and diffing.

## Consequences
- Moving to Postgres later is a connection-string change; the models are plain SQLAlchemy.
- Backups are file copies; `data/` is git-ignored.
