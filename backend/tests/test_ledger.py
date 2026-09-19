from __future__ import annotations

from pathlib import Path

from kotsin_crypto.ledger.db import Ledger, trades


async def test_ledger_roundtrip(tmp_path: Path) -> None:
    led = Ledger(f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
    await led.init()
    try:
        ctl = await led.get_control()
        assert ctl["mode"] == "SHADOW" and ctl["halted"] is False
        await led.set_mode("PAPER")
        await led.set_halt(True, "test")
        ctl = await led.get_control()
        assert ctl["mode"] == "PAPER" and ctl["halted"] is True and ctl["halt_reason"] == "test"

        await led.upsert_wallet("CAN2", {"strategy": "CAN2", "balance": 10_000.0})
        await led.upsert_wallet("CAN2", {"strategy": "CAN2", "balance": 9_990.0})
        assert (await led.load_wallets())["CAN2"]["balance"] == 9_990.0

        await led.insert_signal("s1", "CAN2", "BTCUSD", "LONG", 1, "PAPER_FILLED", "", {"x": 1})
        await led.insert_signal("s1", "CAN2", "BTCUSD", "LONG", 1, "DUP", "", {"x": 2})  # ignored
        await led.insert_order(
            {
                "id": "o1",
                "client_order_id": "s1",
                "strategy": "CAN2",
                "symbol": "BTCUSD",
                "purpose": "ENTRY",
                "status": "FILLED",
                "ts": 2.0,
                "fee": 0.1,
            }
        )
        pos = {
            "id": "p1",
            "strategy": "CAN2",
            "symbol": "BTCUSD",
            "status": "OPEN",
            "opened_ts": 2.0,
            "closed_ts": None,
            "contracts": 3,
        }
        await led.upsert_position(pos)
        assert (await led.load_open_positions())[0]["contracts"] == 3
        pos["status"], pos["closed_ts"] = "CLOSED", 3.0
        await led.upsert_position(pos)
        assert await led.load_open_positions() == []
        await led.insert_trade(
            {
                "id": "t1",
                "position_id": "p1",
                "strategy": "CAN2",
                "symbol": "BTCUSD",
                "closed_ts": 3.0,
                "net": 1.5,
                "r_multiple": 0.5,
                "pnl": 1.6,
            }
        )
        rows = await led.recent(trades, limit=5, order_col="closed_ts")
        assert rows[0]["id"] == "t1" and rows[0]["pnl"] == 1.6
        assert await led.known_client_order_ids() == {"s1"}
        counts = await led.counts()
        assert counts["signals"] == 1 and counts["orders"] == 1 and counts["trades"] == 1
    finally:
        await led.close()
