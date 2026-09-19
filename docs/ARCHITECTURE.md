# Architecture

This file is the build contract. Each step has a *done when* that must be true before the next starts.

## Shape

```
                     Delta Exchange India
  ┌─ public WS ───────────────────────┐  ┌─ private WS ──────────────┐  ┌─ REST ───────────────────┐
  │ v2/ticker · ob_updates · all_trades│  │ orders · positions        │  │ candles backfill          │
  │ candlestick_1m · mark_price        │  │ margins · v2/user_trades  │  │ orders · leverage         │
  │ funding_rate · system_status       │  │                           │  │ positions · balances      │
  └───────────────┬───────────────────┘  └─────────────┬─────────────┘  └─────────────┬────────────┘
                  ▼                                    ▼                              │
   feed/      raw events → Parquet archive (per day, per channel) + L2 book maintainer │
                  ▼                                                                    │
   bars/      1m trade bars (taker buy/sell, VPIN, POC/VA) ⊕ 1m book bars (OFI, Kyle λ, │
              microprice, depth) ⊕ OI ⊕ funding ⊕ mark → UnifiedBar, UTC windows       │
                  ▼                                                                    │
   strategy/  pure: on_bar(ctx, bar) → [Signal]; gates declare on_missing              │
                  ▼                                                                    │
   risk/      the only owner of sizing + exits: wallets, exposure by underlying,        │
              leverage cap, liquidation-distance guard, funding budget, breakers, halt   │
                  ▼                                                                    │
   exec/      gateway: SHADOW | PAPER (fills vs live L2) | LIVE_CAPPED | LIVE           │
              bracket at entry, stop on mark_price, reduce_only exits, reconcile ◀──────┘
                  ▼
   ledger/    SQLite: orders, fills, positions, trades, wallet snapshots, control · JSONL export
   api/       REST + one WebSocket of state diffs → frontend/
   ops/       Telegram alerts + commands, health
   research/  backtester that imports the SAME bars → strategy → risk → paper code
```

All of this is **one asyncio process**. Stages are connected by bounded in-process queues (`bus.py`).

## Infra decisions

| Concern | Choice | Why | Revisit when |
|---|---|---|---|
| Message bus | `asyncio.Queue`s behind a typed bus | one producer, one consumer process, ≤10 symbols | a second *process* needs the same stream ([ADR-0001](adr/0001-single-process-no-kafka.md)) |
| Durability / replay | Parquet archive of every raw WS event | it is also the backtest input and the determinism-test input | never |
| Database | SQLite (WAL) via SQLAlchemy async | single writer; dashboard reads concurrently; ACID for orders | >1 writer process or hosted deploy → Postgres is a URL change ([ADR-0003](adr/0003-sqlite-plus-parquet.md)) |
| Hot state | in memory, persisted on change | halt flag, mode and positions are rows, not Redis keys | — |
| Research | DuckDB over the Parquet archive | columnar, zero-server | — |
| UI serving | backend mounts `frontend/dist` | one port, one systemd unit | — |
| Language | Python 3.12 + asyncio | ccxt / LLM tooling / pandas live here ([ADR-0002](adr/0002-python-asyncio.md)) | — |

## Modules

| Module | Responsibility | Step |
|---|---|---|
| `config.py` | typed, closed settings; unknown keys fail at boot | 1 |
| `bus.py` | typed topics, bounded queues, drop-oldest for market data, block for orders | 1 |
| `venue/delta/auth.py` | REST + WS request signing | 1 |
| `venue/delta/rest.py` | signed REST client, rate-limit budget, 429 handling | 1 |
| `venue/delta/catalogue.py` | product catalogue: id, tick, contract value, margins, fees | 1 |
| `ops/telegram.py` | alerts; no-op when unconfigured; never raises | 1 |
| `venue/delta/ws_public.py` | public feed, heartbeat, reconnect budget, resubscribe | 2 |
| `feed/archive.py` · `feed/book.py` | Parquet writer · L2 snapshot + seq + checksum, resnapshot on gap | 2 |
| `bars/*` | 1m bars, UnifiedBar, resampler, REST backfill warm start | 3 |
| `research/backtest.py` | replay archive through the live code; fee + funding + slippage model | 4 |
| `strategy/*` | keys enum, gates with `on_missing`, CAN2 / FUDKII / BB-squeeze | 5 |
| `risk/*` | wallets, exposure, exits | 6 |
| `exec/gateway.py` · `exec/paper.py` | modes, caps, breaker, halt, idempotency · fills vs live book | 6 |
| `ledger/*` | SQLAlchemy models, JSONL export | 6 |
| `api/*` | REST + WS state diffs | 7 |
| `venue/delta/ws_private.py` · `exec/live.py` · `exec/reconcile.py` | private feed · real orders · reconciliation | 8 |

Boundaries enforced by `import-linter` (`backend/pyproject.toml`): `strategy` may not import `venue`,
`exec`, `ledger`, `api`, `ops` or `feed`; `exec` may not import `strategy`; `venue` is a leaf.

## Feed rules learned in the first soak

- The minute during which the socket (re)connects is missing its first trades: it is tagged
  `source="partial"`, kept for indicators, and excluded from the Delta candle cross-check.
- `HALT` (API / UI / Telegram) blocks new entries **and flattens every open position** at mark; exits
  are always allowed through the gateway even while halted.
- Funding is accrued on the 1 s clock when `now ≥ nfr` for an open position, once per realization.

## Bar verification (finding from the first soak, 2026-09-20)

Our 1m bars are built from the `trades` channel bucketed by **trade time** (`t`). Delta's WebSocket
`candlestick_1m` buckets by **publish time** (`ts`, 0.1–0.6 s later), so a trade near a minute boundary
lands in the adjacent candle: volumes differ in cancelling pairs and opens/closes differ by one trade.
Delta's **REST** candles bucket by trade time and match our bars exactly (602 = 602 …), and the trade
stream is complete (every REST trade is on our tape). Therefore `rest_check` (every 5 min, weight 3) is
the authoritative determinism metric; `candle_check` (vs the WS candle) is kept as a boundary-shift counter.

## Mode and arming

`Mode ∈ {SHADOW, PAPER, LIVE_CAPPED, LIVE}` is a row in the `control` table. `LIVE*` requires an explicit
arm with `armed_until`; a restart after expiry boots into `PAPER`. Every mode change is echoed to Telegram
and shown as a banner in the UI. Halt is one-way from Telegram (`/halt`); resume needs the UI + confirm.

## Build order

| # | Build | Done when |
|---|---|---|
| 1 | Skeleton, `config.py`, bus, Delta REST + catalogue, Telegram, CI, docs | boot fails on a misspelled key; `pytest -m live` sees BTCUSD id 27; CI green |
| 2 | `ws_public.py`, `feed/archive.py`, `feed/book.py` for BTCUSD/ETHUSD/SOLUSD on mainnet public feed | 24h unattended: zero unrecovered gaps, archive complete, checksums verified |
| 3 | `bars/*` + backfill | **determinism**: bars rebuilt from the archive are byte-identical to live bars and match Delta's 1m candles within tolerance over 7 days |
| 4 | `research/backtest.py` + cost model | a null strategy backtests to exactly −fees −funding |
| 5 | CAN2-crypto → FUDKII-crypto → BB-squeeze, each with `docs/strategies/<KEY>.md` | backtest artefact, walk-forward, within-day permutation test, ≥300 OOS trades, net edge > 0.15%/trade |
| 6 | `risk/*`, `exec/gateway.py` + `paper.py`, `ledger/*` | PAPER ≥2 weeks / ≥100 trades; paper-vs-backtest divergence explained |
| 7 | API + WS + the six frontend pages | all pages live off the paper run |
| 8 | `ws_private.py`, `exec/live.py`, `reconcile.py`: testnet LIVE, then mainnet LIVE_CAPPED (1 contract, daily budget) | 5 kill -9 restarts with open positions → zero unreconciled; live-vs-paper slippage audited |
| 9 | XAUT, funding/basis strategies, LLM committee, options, second venue | same path each time |

## Frontend pages

1. **Overview** — mode banner (+ armed-until), wallets, open positions with liquidation distance, day P&L.
2. **Chart** — `lightweight-charts`: candles, signals, fills, SL/TP ladders; symbol/timeframe switch.
3. **Signals** — every candidate incl. rejections, with the gate that killed it.
4. **Trades** — ledger with R-multiple, MFE/MAE, fees, funding, paper-vs-live slippage.
5. **Risk** — exposure by underlying, breakers, Halt button with confirm.
6. **System** — WS health, book gaps/resnapshots, rate-limit budget, last reconciliation, archive lag.
