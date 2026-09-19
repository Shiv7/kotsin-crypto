"""SQLite ledger (WAL) over SQLAlchemy async. One writer — the engine. Rich records are stored as JSON
next to the columns we query on; the API returns them verbatim."""

from __future__ import annotations

import json
import time
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

meta = sa.MetaData()

control = sa.Table(
    "control",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("mode", sa.String, nullable=False),
    sa.Column("armed_until", sa.Float),
    sa.Column("halted", sa.Boolean, nullable=False, default=False),
    sa.Column("halt_reason", sa.String, default=""),
    sa.Column("updated_ts", sa.Float, nullable=False),
)
wallets = sa.Table(
    "wallets",
    meta,
    sa.Column("strategy", sa.String, primary_key=True),
    sa.Column("json", sa.Text, nullable=False),
    sa.Column("updated_ts", sa.Float, nullable=False),
)
signals = sa.Table(
    "signals",
    meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("signal_id", sa.String, unique=True, nullable=False),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("side", sa.String, nullable=False),
    sa.Column("ts", sa.Integer, nullable=False),
    sa.Column("decision", sa.String, nullable=False),
    sa.Column("decision_reason", sa.String, default=""),
    sa.Column("json", sa.Text, nullable=False),
    sa.Column("created_ts", sa.Float, nullable=False),
)
orders = sa.Table(
    "orders",
    meta,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("client_order_id", sa.String, unique=True, nullable=False),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("purpose", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
)
positions = sa.Table(
    "positions",
    meta,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("opened_ts", sa.Float, nullable=False),
    sa.Column("closed_ts", sa.Float),
    sa.Column("json", sa.Text, nullable=False),
)
trades = sa.Table(
    "trades",
    meta,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("position_id", sa.String, nullable=False),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("symbol", sa.String, nullable=False),
    sa.Column("closed_ts", sa.Float, nullable=False),
    sa.Column("net", sa.Float, nullable=False),
    sa.Column("r_multiple", sa.Float, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
)
wallet_snapshots = sa.Table(
    "wallet_snapshots",
    meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
)
events = sa.Table(
    "events",
    meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
)
health = sa.Table(
    "health",
    meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Float, nullable=False),
    sa.Column("json", sa.Text, nullable=False),
)


def _j(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


class Ledger:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, future=True)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            if self.url.startswith("sqlite"):
                await conn.execute(sa.text("PRAGMA journal_mode=WAL"))
                await conn.execute(sa.text("PRAGMA synchronous=NORMAL"))
            await conn.run_sync(meta.create_all)
            row = (await conn.execute(sa.select(control).where(control.c.id == 1))).first()
            if row is None:
                await conn.execute(
                    control.insert().values(
                        id=1, mode="SHADOW", halted=False, halt_reason="", updated_ts=time.time()
                    )
                )

    async def close(self) -> None:
        await self.engine.dispose()

    # ---- control ------------------------------------------------------------------------------
    async def get_control(self) -> dict[str, Any]:
        async with self.engine.connect() as conn:
            row = (
                (await conn.execute(sa.select(control).where(control.c.id == 1))).mappings().first()
            )
            return dict(row) if row else {}

    async def set_mode(self, mode: str, armed_until: float | None = None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                control.update()
                .where(control.c.id == 1)
                .values(mode=mode, armed_until=armed_until, updated_ts=time.time())
            )

    async def set_halt(self, halted: bool, reason: str = "") -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                control.update()
                .where(control.c.id == 1)
                .values(halted=halted, halt_reason=reason, updated_ts=time.time())
            )

    # ---- wallets --------------------------------------------------------------------------------
    async def upsert_wallet(self, strategy: str, data: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            exists = (
                await conn.execute(
                    sa.select(wallets.c.strategy).where(wallets.c.strategy == strategy)
                )
            ).first()
            if exists:
                await conn.execute(
                    wallets.update()
                    .where(wallets.c.strategy == strategy)
                    .values(json=_j(data), updated_ts=time.time())
                )
            else:
                await conn.execute(
                    wallets.insert().values(
                        strategy=strategy, json=_j(data), updated_ts=time.time()
                    )
                )

    async def load_wallets(self) -> dict[str, dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sa.select(wallets))).mappings().all()
            return {r["strategy"]: json.loads(r["json"]) for r in rows}

    async def snapshot_wallet(
        self, strategy: str, data: dict[str, Any], ts: float | None = None
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                wallet_snapshots.insert().values(
                    ts=ts or time.time(), strategy=strategy, json=_j(data)
                )
            )

    # ---- signals / orders / positions / trades --------------------------------------------------
    async def insert_signal(
        self,
        signal_id: str,
        strategy: str,
        symbol: str,
        side: str,
        ts: int,
        decision: str,
        reason: str,
        data: dict[str, Any],
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(signals)
                .prefix_with("OR IGNORE")
                .values(
                    signal_id=signal_id,
                    strategy=strategy,
                    symbol=symbol,
                    side=side,
                    ts=ts,
                    decision=decision,
                    decision_reason=reason,
                    json=_j(data),
                    created_ts=time.time(),
                )
            )

    async def insert_order(self, order: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(orders)
                .prefix_with("OR IGNORE")
                .values(
                    id=order["id"],
                    client_order_id=order["client_order_id"],
                    strategy=order["strategy"],
                    symbol=order["symbol"],
                    purpose=order["purpose"],
                    status=order["status"],
                    ts=order["ts"],
                    json=_j(order),
                )
            )

    async def upsert_position(self, pos: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            exists = (
                await conn.execute(sa.select(positions.c.id).where(positions.c.id == pos["id"]))
            ).first()
            vals = {
                "strategy": pos["strategy"],
                "symbol": pos["symbol"],
                "status": pos["status"],
                "opened_ts": pos["opened_ts"],
                "closed_ts": pos.get("closed_ts"),
                "json": _j(pos),
            }
            if exists:
                await conn.execute(
                    positions.update().where(positions.c.id == pos["id"]).values(**vals)
                )
            else:
                await conn.execute(positions.insert().values(id=pos["id"], **vals))

    async def load_open_positions(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(sa.select(positions.c.json).where(positions.c.status == "OPEN"))
            ).all()
            return [json.loads(r[0]) for r in rows]

    async def insert_trade(self, trade: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(trades)
                .prefix_with("OR IGNORE")
                .values(
                    id=trade["id"],
                    position_id=trade["position_id"],
                    strategy=trade["strategy"],
                    symbol=trade["symbol"],
                    closed_ts=trade["closed_ts"],
                    net=trade["net"],
                    r_multiple=trade["r_multiple"],
                    json=_j(trade),
                )
            )

    async def known_client_order_ids(self) -> set[str]:
        async with self.engine.connect() as conn:
            return {r[0] for r in (await conn.execute(sa.select(orders.c.client_order_id))).all()}

    # ---- events / health ------------------------------------------------------------------------
    async def event(self, kind: str, data: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(events.insert().values(ts=time.time(), kind=kind, json=_j(data)))

    async def insert_health(self, data: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(health.insert().values(ts=time.time(), json=_j(data)))

    # ---- reads for the API -----------------------------------------------------------------------
    async def recent(
        self, table: sa.Table, *, limit: int = 100, order_col: str = "ts"
    ) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            col = table.c[order_col]
            rows = (
                (await conn.execute(sa.select(table).order_by(col.desc()).limit(limit)))
                .mappings()
                .all()
            )
            out = []
            for r in rows:
                d = dict(r)
                if "json" in d:
                    d = {**{k: v for k, v in d.items() if k != "json"}, **json.loads(d["json"])}
                out.append(d)
            return out

    async def counts(self) -> dict[str, int]:
        async with self.engine.connect() as conn:
            out = {}
            for t in (signals, orders, positions, trades, wallet_snapshots, events, health):
                out[t.name] = (
                    await conn.execute(sa.select(sa.func.count()).select_from(t))
                ).scalar_one()
            return out
