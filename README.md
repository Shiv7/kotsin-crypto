# kotsin-crypto

Personal crypto trading engine for **Delta Exchange India**. One asyncio backend, one React frontend.
No Kafka, no Redis, no Mongo — one process, one port, one SQLite file, one Parquet directory.

Built from the lessons of the Kotsin NSE stack, with **zero code dependency on it**: separate repo, new
package, UTC everywhere, no broker or session assumptions inside strategy code. The rules distilled from
that stack are in [`docs/LEARNINGS.md`](docs/LEARNINGS.md); the design and build order are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); everything verified about the venue is in
[`docs/DELTA_INDIA_FACTS.md`](docs/DELTA_INDIA_FACTS.md).

## Principles

1. **One process, one port.** feed → bars → strategies → risk → gateway → ledger → API run in a single
   `asyncio` process. Split only when a measurement says so ([ADR-0001](docs/adr/0001-single-process-no-kafka.md)).
2. **The exchange is the source of truth.** Positions and orders are reconciled against Delta on boot and
   continuously; local state is a cache.
3. **Strategies are pure.** `on_bar(ctx, bar) -> list[Signal]` — no I/O, no clock, no venue. Enforced by
   `import-linter`, not by convention.
4. **Nothing goes live without an artefact.** Backtest → paper → capped live, each with a written result
   in `docs/strategies/`.
5. **Config is typed and closed.** An unknown key fails the boot, not the trade.
6. **Mode is state, not an env var.** `SHADOW / PAPER / LIVE_CAPPED / LIVE` live in the control table;
   `LIVE` must be armed with an expiry and is announced on Telegram.

## Layout

```
backend/    Python 3.12 · FastAPI + asyncio · uv-managed      (kotsin_crypto/)
frontend/   React 18 · Vite · TypeScript · Tailwind · Zustand (served by the backend in prod)
deploy/     Dockerfile (one image), docker-compose.yml (one service), systemd unit
docs/       ARCHITECTURE, DELTA_INDIA_FACTS, LEARNINGS, ADRs, strategy docs
```

## Quickstart

```bash
# backend
cp .env.example backend/.env          # keys optional: the paper engine only uses PUBLIC endpoints; .env is git-ignored
cd backend
uv sync                               # creates .venv with Python 3.12 (arm64/x86 chosen by uv)
uv run pytest -m "not live" -q        # unit tests, offline
uv run pytest -m live -q              # hits Delta's public API (no key needed)
KC_DELTA_ENV=mainnet uv run kotsin-crypto   # http://127.0.0.1:8400  (UI) · /api/health · /api/system
curl -X POST localhost:8400/api/control/mode -H 'Content-Type: application/json' -d '{"mode":"PAPER"}'

# frontend (dev server proxies /api and /ws to :8400)
cd ../frontend && npm install && npm run dev

# both in one container
docker compose -f deploy/docker-compose.yml up --build
```

## Status

| Step | What | Done when | State |
|---|---|---|---|
| 1 | Skeleton, closed config, bus, Delta REST client + product catalogue, Telegram, CI | boot fails on a misspelled key; CI green; `pytest -m live` sees BTCUSD | ✅ |
| 2 | Public WS feed + JSONL archive + L2 book | 24h unattended, zero unrecovered gaps | 🟡 built — 24h soak running |
| 3 | 1m trade/book/OI bars, UnifiedBar, REST backfill | live 1m bars match Delta's `candlestick_1m`; archive replay byte-identical | 🟡 built — live check vs Delta candles running |
| 4 | Backtester + cost model | a null strategy backtests to exactly −fees −funding | ⬜ |
| 5 | Strategies CAN2 → FUDKII → BB-squeeze | artefact per strategy, ≥300 OOS trades, net edge > 0.15%/trade | 🟡 CAN2-crypto built (pipeline-test parameters, no edge claim) |
| 6 | Risk + gateway (PAPER) + ledger | ≥2 weeks / ≥100 paper trades | 🟡 built — first 24h paper soak running |
| 7 | API/WS + frontend pages | all six pages live off the paper run | 🟡 built |
| 8 | Private WS, live orders, reconciliation → testnet LIVE → mainnet LIVE_CAPPED | 5 kill -9 restarts with open positions, zero unreconciled | ⬜ |

Full table with rationale: `docs/ARCHITECTURE.md`.
