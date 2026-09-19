"""Public WebSocket feed (verified against the live endpoint 2026-09-20).

The public socket uses the *short* channel names: ``ticker``, ``ob_l1``, ``ob_l2``, ``ob_updates``,
``trades``, ``mark_price`` (symbols ``MARK:<sym>``), ``candlestick_1m``, ``funding_rate``,
``spot_price``, ``system_status`` (no symbols). The long names from the older docs (``v2/ticker``,
``l2_orderbook``, ``all_trades``) are rejected there.

Rules: ``enable_heartbeat`` right after connect (server heartbeats every ~5 s; the venue drops idle
connections after 60 s, we reconnect after 45 s of silence); exponential backoff capped at 60 s keeps
us far inside the 150 connections / 5 min / IP budget; every subscription is re-sent on reconnect;
the message callback is synchronous and must be cheap (archive append + parse + dispatch).
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import structlog
import websockets

log = structlog.get_logger("delta.ws")

OnMessage = Callable[[dict[str, Any], str, int], None]


def channels_for(symbols: list[str]) -> list[dict[str, Any]]:
    return [
        {"name": "ticker", "symbols": symbols},
        {"name": "ob_l1", "symbols": symbols},
        {"name": "ob_l2", "symbols": symbols},
        {"name": "trades", "symbols": symbols},
        {"name": "mark_price", "symbols": [f"MARK:{s}" for s in symbols]},
        {"name": "candlestick_1m", "symbols": symbols},
        {"name": "funding_rate", "symbols": symbols},
        {"name": "system_status"},
    ]


class DeltaPublicWS:
    SILENCE_TIMEOUT_S = 45.0

    def __init__(
        self,
        url: str,
        channels: list[dict[str, Any]],
        on_message: OnMessage,
        *,
        force_ipv4: bool = True,
    ) -> None:
        self.url = url
        self.channels = channels
        self.on_message = on_message
        self.force_ipv4 = force_ipv4
        self.connected = False
        self.connect_ts: float | None = None
        self.reconnects = 0
        self.errors = 0
        self.last_heartbeat_ts: float | None = None
        self.last_msg_ts: float | None = None
        self.counts: dict[str, int] = defaultdict(int)
        self.last_ts: dict[str, float] = {}
        self.subscribed: list[dict[str, Any]] = []
        self.last_error: str = ""

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(
                    self.url,
                    max_size=16_000_000,
                    ping_interval=None,
                    open_timeout=15,
                    family=socket.AF_INET if self.force_ipv4 else 0,
                ) as ws:
                    self.connected = True
                    self.connect_ts = time.time()
                    await ws.send(json.dumps({"type": "enable_heartbeat"}))
                    await ws.send(
                        json.dumps({"type": "subscribe", "payload": {"channels": self.channels}})
                    )
                    log.info(
                        "ws_connected", url=self.url, channels=[c["name"] for c in self.channels]
                    )
                    backoff = 1.0
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.SILENCE_TIMEOUT_S)
                        except TimeoutError:
                            raise ConnectionError(
                                f"no message for {self.SILENCE_TIMEOUT_S}s"
                            ) from None
                        recv_us = time.time_ns() // 1000
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        self.last_msg_ts = recv_us / 1e6
                        msg = json.loads(raw)
                        t = msg.get("type", "?")
                        if t == "heartbeat":
                            self.last_heartbeat_ts = self.last_msg_ts
                            continue
                        if t == "subscriptions":
                            self.subscribed = msg.get("channels", [])
                            bad = [c for c in self.subscribed if "error" in c]
                            if bad:
                                self.errors += 1
                                log.error("ws_subscription_rejected", channels=bad)
                            continue
                        if t == "error":
                            self.errors += 1
                            self.last_error = str(msg.get("message"))
                            log.error("ws_error", message=msg.get("message"))
                            continue
                        self.counts[t] += 1
                        self.last_ts[t] = self.last_msg_ts
                        try:
                            self.on_message(msg, raw, recv_us)
                        except Exception as exc:  # a bad message must not drop the connection
                            self.errors += 1
                            self.last_error = f"{t}: {exc}"
                            log.exception("ws_handler_failed", type=t)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.reconnects += 1
                self.last_error = str(exc)
                delay = backoff + random.uniform(0, backoff / 2)
                log.warning("ws_disconnected", error=str(exc), retry_in_s=round(delay, 1))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
        self.connected = False

    def stats(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        return {
            "connected": self.connected,
            "connected_for_s": round(now - self.connect_ts, 1)
            if self.connected and self.connect_ts
            else None,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "last_error": self.last_error,
            "heartbeat_age_s": round(now - self.last_heartbeat_ts, 1)
            if self.last_heartbeat_ts
            else None,
            "channels": {
                t: {"count": c, "age_s": round(now - self.last_ts[t], 1)}
                for t, c in self.counts.items()
            },
        }
