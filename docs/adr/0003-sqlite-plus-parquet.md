# ADR-0003 — SQLite for the ledger, Parquet for market data

**Status:** accepted · 2026-09-20

## Context
Orders, fills, positions and the control row need ACID and a single writer. Market data needs append
speed and columnar reads for research. Neither needs a server on a personal box.

## Decision
- `data/kotsin_crypto.db`: SQLite in WAL mode via SQLAlchemy async — orders, fills, positions, trades,
  wallet snapshots, signals (incl. rejections), control.
- `data/archive/<channel>/<yyyy-mm-dd>.parquet`: every raw WS event, flushed every few seconds.
- DuckDB queries the archive directly for backtests and reports.
- JSONL export of closed trades (same shape as the NSE ledger, plus funding/leverage/mark fields) for
  durability and diffing.

## Consequences
- Moving to Postgres later is a connection-string change; the models are plain SQLAlchemy.
- Backups are file copies; `data/` is git-ignored.
