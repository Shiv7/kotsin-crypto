"""Private WebSocket (``settings.endpoints.ws_private``): ``key-auth`` handshake, then ``orders``,
``positions``, ``v2/user_trades`` and ``margins`` for the configured symbols.

Payloads (docs.delta.exchange, read 2026-09-20):
    orders          {"type":"orders","action":"create|update|delete","reason":"fill|stop_update|stop_trigger|
                     stop_cancel|liquidation|self_trade|null","symbol","product_id","order_id","client_order_id",
                     "size","unfilled_size","average_fill_price","limit_price","side","cancellation_reason",
                     "stop_order_type","stop_price",...}
    positions       {"type":"positions","action","reason","symbol","product_id","size" (signed contracts),
                     "margin","entry_price",...}
    v2/user_trades  {"type":"v2/user_trades","sy","f" fill_id,"R" reason (normal|adl|liquidation),"u","o" order_id,
                     "S" side,"s" size,"p" price,"po" position after,"r" role (taker|maker),"c" client_order_id,
                     "t" ts µs,"se" seq}
    margins         {"type":"margins", ... "timestamp"}  (kept raw)

Auth failures ({"type":"key-auth","success":false,"status":"ip_not_whitelisted"|"invalid_signature"|
"api_key_not_found"|"request_expired",...}) are logged at ERROR and retried with the same exponential
backoff as a disconnect — never tight-looped. This feed is never the source of truth: the reconciler
polls REST.
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
import websockets

from .auth import ws_auth_message

log = structlog.get_logger("delta.ws.private")


class AuthError(Exception):
    pass


@dataclass(slots=True)
class OrderEvt:
    action: str
    reason: str | None
    symbol: str
    product_id: int | None
    order_id: int | None
    client_order_id: str | None
    size: int | None
    unfilled_size: int | None
    average_fill_price: float | None
    side: str | None
    state: str | None
    stop_order_type: str | None
    stop_price: float | None
    raw: dict[str, Any]


@dataclass(slots=True)
class PositionEvt:
    action: str
    symbol: str
    product_id: int | None
    size: int  # signed contracts
    entry_price: float | None
    margin: float | None
    raw: dict[str, Any]


@dataclass(slots=True)
class UserTradeEvt:
    symbol: str
    fill_id: str
    order_id: int | None
    client_order_id: str | None
    side: str
    size: int
    price: float
    role: str | None
    reason: str | None
    position_after: int | None
    ts_us: int


@dataclass(slots=True)
class MarginEvt:
    ts_us: int
    raw: dict[str, Any]


PrivateEvent = OrderEvt | PositionEvt | UserTradeEvt | MarginEvt


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        return None if v in (None, "") else int(float(v))
    except (TypeError, ValueError):
        return None


def parse_private(msg: dict[str, Any]) -> PrivateEvent | None:
    t = msg.get("type")
    try:
        if t == "orders":
            return OrderEvt(
                action=str(msg.get("action") or ""),
                reason=msg.get("reason") or None,
                symbol=str(msg.get("symbol") or msg.get("product_symbol") or ""),
                product_id=_i(msg.get("product_id")),
                order_id=_i(msg.get("order_id") or msg.get("id")),
                client_order_id=msg.get("client_order_id") or None,
                size=_i(msg.get("size")),
                unfilled_size=_i(msg.get("unfilled_size")),
                average_fill_price=_f(msg.get("average_fill_price")),
                side=msg.get("side"),
                state=msg.get("state"),
                stop_order_type=msg.get("stop_order_type") or None,
                stop_price=_f(msg.get("stop_price")),
                raw=msg,
            )
        if t == "positions":
            return PositionEvt(
                action=str(msg.get("action") or ""),
                symbol=str(msg.get("symbol") or msg.get("product_symbol") or ""),
                product_id=_i(msg.get("product_id")),
                size=_i(msg.get("size")) or 0,
                entry_price=_f(msg.get("entry_price")),
                margin=_f(msg.get("margin")),
                raw=msg,
            )
        if t == "v2/user_trades":
            return UserTradeEvt(
                symbol=str(msg["sy"]),
                fill_id=str(msg.get("f")),
                order_id=_i(msg.get("o")),
                client_order_id=msg.get("c") or None,
                side=str(msg.get("S")),
                size=_i(msg.get("s")) or 0,
                price=float(msg["p"]),
                role=msg.get("r"),
                reason=msg.get("R"),
                position_after=_i(msg.get("po")),
                ts_us=_i(msg.get("t")) or 0,
            )
        if t == "margins":
            return MarginEvt(ts_us=_i(msg.get("timestamp")) or 0, raw=msg)
    except (KeyError, TypeError, ValueError):
        return None
    return None


OnPrivate = Callable[[PrivateEvent, dict[str, Any]], None]


class DeltaPrivateWS:
    SILENCE_TIMEOUT_S = 45.0
    AUTH_TIMEOUT_S = 10.0

    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
        symbols: list[str],
        on_event: OnPrivate,
        *,
        force_ipv4: bool = True,
    ) -> None:
        self.url = url
        self._key = api_key
        self._secret = api_secret
        self.symbols = symbols
        self.on_event = on_event
        self.force_ipv4 = force_ipv4
        self.connected = False
        self.authenticated = False
        self.connect_ts: float | None = None
        self.reconnects = 0
        self.auth_failures = 0
        self.errors = 0
        self.last_error = ""
        self.last_heartbeat_ts: float | None = None
        self.counts: dict[str, int] = defaultdict(int)
        self.last_ts: dict[str, float] = {}

    def channels(self) -> list[dict[str, Any]]:
        return [
            {"name": "orders", "symbols": self.symbols},
            {"name": "positions", "symbols": self.symbols},
            {"name": "v2/user_trades", "symbols": self.symbols},
            {"name": "margins"},
        ]

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
                    await ws.send(json.dumps(ws_auth_message(self._key, self._secret)))
                    await self._await_auth(ws)
                    await ws.send(
                        json.dumps({"type": "subscribe", "payload": {"channels": self.channels()}})
                    )
                    log.info("private_ws_authenticated", symbols=self.symbols)
                    backoff = 1.0
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.SILENCE_TIMEOUT_S)
                        except TimeoutError:
                            raise ConnectionError(
                                f"no message for {self.SILENCE_TIMEOUT_S}s"
                            ) from None
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        msg = json.loads(raw)
                        t = msg.get("type", "?")
                        now = time.time()
                        if t == "heartbeat":
                            self.last_heartbeat_ts = now
                            continue
                        if t == "subscriptions":
                            bad = [c for c in msg.get("channels", []) if "error" in c]
                            if bad:
                                self.errors += 1
                                self.last_error = f"subscription rejected: {bad}"
                                log.error("private_ws_subscription_rejected", channels=bad)
                            continue
                        if t == "error":
                            self.errors += 1
                            self.last_error = str(msg.get("message"))
                            log.error("private_ws_error", message=msg.get("message"))
                            continue
                        self.counts[t] += 1
                        self.last_ts[t] = now
                        evt = parse_private(msg)
                        if evt is None:
                            continue
                        try:
                            self.on_event(evt, msg)
                        except Exception:
                            self.errors += 1
                            log.exception("private_ws_handler_failed", type=t)
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                self.connected = self.authenticated = False
                self.auth_failures += 1
                self.last_error = f"auth: {exc}"
                delay = min(60.0, backoff * 4) + random.uniform(0, 2)
                log.error("private_ws_auth_failed", error=str(exc), retry_in_s=round(delay, 1))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
            except Exception as exc:
                self.connected = self.authenticated = False
                self.reconnects += 1
                self.last_error = str(exc)
                delay = backoff + random.uniform(0, backoff / 2)
                log.warning("private_ws_disconnected", error=str(exc), retry_in_s=round(delay, 1))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
        self.connected = self.authenticated = False

    async def _await_auth(self, ws: Any) -> None:
        deadline = time.time() + self.AUTH_TIMEOUT_S
        while time.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=self.AUTH_TIMEOUT_S)
            if isinstance(raw, bytes):
                raw = raw.decode()
            msg = json.loads(raw)
            if msg.get("type") == "key-auth":
                if msg.get("success"):
                    self.authenticated = True
                    return
                raise AuthError(f"{msg.get('status')}: {msg.get('message')}")
            if msg.get("type") == "heartbeat":
                self.last_heartbeat_ts = time.time()
        raise AuthError("no key-auth response within timeout")

    def stats(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        return {
            "connected": self.connected,
            "authenticated": self.authenticated,
            "reconnects": self.reconnects,
            "auth_failures": self.auth_failures,
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
