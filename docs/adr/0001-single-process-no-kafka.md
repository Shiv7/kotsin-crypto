# ADR-0001 — One asyncio process, no Kafka / Redis / Mongo

**Status:** accepted · 2026-09-20

## Context
The NSE stack ran seven services over Kafka, MongoDB and Redis on one box. That bought independent
deployability nobody used, and cost a thundering-herd restart, a self-inflicted database restart loop
and 19 GB of heap ceilings on a 2 GB machine. The crypto engine watches ≤10 symbols from one venue.

## Decision
One Python asyncio process: feed → bars → strategies → risk → gateway → ledger → API. Stages talk over
bounded in-process queues (`bus.py`). Durability and replay come from a Parquet archive of raw events,
not from a broker. State is SQLite (WAL) + in-memory.

## Consequences
- Deploy = one systemd unit / one container with a memory limit.
- Backtester imports the live code path; there is no second implementation to drift.
- A crash takes the whole engine down — acceptable because the exchange holds the positions and
  `reconcile.py` restores the view on boot; bracket orders resting on the exchange keep the stop alive.
- Revisit when a second process needs the same stream (e.g. a separate research worker on the live
  feed): the first step is Redis Streams, not Kafka.
