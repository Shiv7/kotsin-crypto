# ADR-0002 — Python 3.12 + asyncio for the backend

**Status:** accepted · 2026-09-20

## Context
The NSE engines are Java/Spring, but 61 classes in the signal engine and 45 in the executor hard-code
IST sessions, lot sizes and one broker's contracts. Porting them is a retrofit, not a reuse. The crypto
tooling worth reusing (ccxt, pandas/DuckDB research, LLM frameworks) is Python.

## Decision
Python 3.12, `uv`-managed, single asyncio event loop, FastAPI for the API, `websockets` + `httpx` for the
venue, SQLAlchemy async over SQLite, pyarrow/DuckDB for research. The maths from the NSE stack is
re-implemented against Delta's data model with golden-vector tests; no code is copied.

## Consequences
- 3–10 symbols of 100 ms L2 updates are well within one loop's budget; the bus drops stale book
  updates under pressure and counts the drops.
- CPU-heavy research runs in DuckDB/numpy, not in the event loop.
- `import-linter` and `ruff` replace the compiler as the boundary keeper.
