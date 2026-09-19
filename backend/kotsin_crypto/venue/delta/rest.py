"""Signed REST client for Delta India with a local rate-limit budget.

Public endpoints need no key. Private reads (balances, positions, orders, fills) and private writes
(orders, cancels, leverage, close-all) are signed over the exact bytes sent. Only ``exec/live.py``
calls the writes, and only in LIVE modes behind the gateway's caps.

Rate limit: 20,000 units per rolling 5 minutes; weights per docs/DELTA_INDIA_FACTS.md. The budget is
tracked locally so the engine degrades before the venue starts returning 429s.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from ... import __version__
from ...config import Settings
from .auth import rest_headers

log = structlog.get_logger("delta.rest")

USER_AGENT = f"kotsin-crypto/{__version__}"
WINDOW_S = 300
QUOTA_UNITS = 20_000


class DeltaApiError(RuntimeError):
    """Venue error. Delta's error payload is ``{"code": "insufficient_margin", "context": {...}}``;
    ``code`` and ``context`` are surfaced so callers can branch on them."""

    def __init__(self, status: int, error: Any) -> None:
        code, context = None, None
        if isinstance(error, dict):
            code = error.get("code")
            context = error.get("context")
        msg = f"delta api error {status}: {code or error}"
        if isinstance(context, dict) and context.get("client_ip"):
            # ip_not_whitelisted_for_api_key: tell the operator exactly which address to whitelist
            msg += f" (client_ip={context['client_ip']} — whitelist this IP on the key)"
        elif context:
            msg += f" {context}"
        super().__init__(msg)
        self.status = status
        self.error = error
        self.code = code
        self.context = context


class DeltaRateLimited(DeltaApiError):
    def __init__(self, reset_ms: int, *, local: bool) -> None:
        super().__init__(
            429, f"rate limited ({'local budget' if local else 'venue'}), reset in {reset_ms} ms"
        )
        self.reset_ms = reset_ms
        self.local = local


def weight_for(method: str, path: str) -> int:
    if "/batch" in path:
        return 25
    if any(seg in path for seg in ("/orders/history", "/fills", "/wallet/transactions")):
        return 10
    if method.upper() != "GET":
        return 5
    return 3


class RateBudget:
    def __init__(self, quota: int = QUOTA_UNITS, window_s: int = WINDOW_S) -> None:
        self.quota = quota
        self.window_s = window_s
        self._spent: deque[tuple[float, int]] = deque()

    def _prune(self, now: float) -> None:
        while self._spent and self._spent[0][0] < now - self.window_s:
            self._spent.popleft()

    def used(self, now: float | None = None) -> int:
        self._prune(time.monotonic() if now is None else now)
        return sum(units for _, units in self._spent)

    def remaining(self) -> int:
        return self.quota - self.used()

    def charge(self, units: int) -> None:
        now = time.monotonic()
        used = self.used(now)
        if used + units > self.quota:
            oldest_ts = self._spent[0][0] if self._spent else now
            reset_ms = int(max(0.0, oldest_ts + self.window_s - now) * 1000)
            raise DeltaRateLimited(reset_ms, local=True)
        self._spent.append((now, units))
        if used + units > self.quota * 0.8:
            log.warning("rate_budget_high", used=used + units, quota=self.quota)


class DeltaRest:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        if client is None:
            # IPv4-only by default: Delta whitelists keys by IP and this box alternates between an
            # IPv4 and a rotating IPv6 privacy address; binding to 0.0.0.0 pins one stable address.
            transport = (
                httpx.AsyncHTTPTransport(local_address="0.0.0.0")
                if settings.delta_force_ipv4
                else None
            )
            client = httpx.AsyncClient(
                base_url=settings.endpoints.rest,
                timeout=httpx.Timeout(15.0, connect=5.0),
                transport=transport,
            )
        self._client = client
        self.budget = RateBudget()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        query = (
            "?" + urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
            if params
            else ""
        )
        body = json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self._settings.has_api_keys:
                raise DeltaApiError(
                    401, "API key/secret not configured (KC_DELTA_API_KEY / _SECRET)"
                )
            assert self._settings.delta_api_key and self._settings.delta_api_secret
            headers |= rest_headers(
                self._settings.delta_api_key.get_secret_value(),
                self._settings.delta_api_secret.get_secret_value(),
                method,
                path,
                query,
                body,
            )
        self.budget.charge(weight_for(method, path))
        resp = await self._client.request(
            method, path + query, content=body or None, headers=headers
        )
        if resp.status_code == 429:
            reset_ms = int(resp.headers.get("X-RATE-LIMIT-RESET", "5000"))
            log.warning("rate_limited_by_venue", path=path, reset_ms=reset_ms)
            raise DeltaRateLimited(reset_ms, local=False)
        try:
            data = resp.json()
        except ValueError as exc:
            raise DeltaApiError(resp.status_code, resp.text[:200]) from exc
        if resp.status_code >= 400 or data.get("success") is False:
            raise DeltaApiError(resp.status_code, data.get("error", data))
        return data

    # ---- public --------------------------------------------------------------------------------

    async def products_page(
        self,
        *,
        contract_types: str | None = None,
        states: str | None = "live",
        page_size: int = 500,
        after: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v2/products",
            params={
                "contract_types": contract_types,
                "states": states,
                "page_size": page_size,
                "after": after,
                **extra,
            },
        )

    async def all_products(self, **filters: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            page = await self.products_page(after=after, **filters)
            out.extend(page.get("result", []))
            after = (page.get("meta") or {}).get("after")
            if not after:
                return out

    async def ticker(self, symbol: str) -> dict[str, Any]:
        return (await self._request("GET", f"/v2/tickers/{symbol}"))["result"]

    async def tickers(
        self, contract_types: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        return (
            await self._request(
                "GET", "/v2/tickers", params={"contract_types": contract_types, **params}
            )
        )["result"]

    async def candles(
        self, symbol: str, resolution: str, start: int, end: int
    ) -> list[dict[str, Any]]:
        """``symbol`` may carry the ``MARK:`` / ``OI:`` / ``FUNDING:`` prefix; ``start``/``end`` are unix seconds."""
        return (
            await self._request(
                "GET",
                "/v2/history/candles",
                params={"symbol": symbol, "resolution": resolution, "start": start, "end": end},
            )
        )["result"]

    async def orderbook(self, symbol: str, depth: int = 25) -> dict[str, Any]:
        return (await self._request("GET", f"/v2/l2orderbook/{symbol}", params={"depth": depth}))[
            "result"
        ]

    async def trades(self, symbol: str) -> list[dict[str, Any]]:
        result = (await self._request("GET", f"/v2/trades/{symbol}"))["result"]
        return result.get("trades", result) if isinstance(result, dict) else result

    # ---- private reads ---------------------------------------------------------------------------

    async def wallet_balances(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/v2/wallet/balances", auth=True))["result"]

    async def positions(self) -> list[dict[str, Any]]:
        """Margined positions: ``size`` is SIGNED contracts (+long / −short), prices are strings."""
        return (await self._request("GET", "/v2/positions/margined", auth=True))["result"]

    async def open_orders(
        self, product_ids: str | None = None, states: str = "open,pending"
    ) -> list[dict[str, Any]]:
        return (
            await self._request(
                "GET",
                "/v2/orders",
                params={"product_ids": product_ids, "states": states},
                auth=True,
            )
        )["result"]

    async def get_order(self, order_id: int) -> dict[str, Any]:
        return (await self._request("GET", f"/v2/orders/{order_id}", auth=True))["result"]

    async def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any]:
        return (
            await self._request("GET", f"/v2/orders/client_order_id/{client_order_id}", auth=True)
        )["result"]

    async def fills(
        self, product_ids: str | None = None, page_size: int = 50
    ) -> list[dict[str, Any]]:
        return (
            await self._request(
                "GET",
                "/v2/fills",
                params={"product_ids": product_ids, "page_size": min(page_size, 50)},
                auth=True,
            )
        )["result"]

    async def get_leverage(self, product_id: int) -> dict[str, Any]:
        return (
            await self._request("GET", f"/v2/products/{product_id}/orders/leverage", auth=True)
        )["result"]

    # ---- private writes (LIVE modes only, always via exec/live.py) ----------------------------

    async def set_leverage(self, product_id: int, leverage: float | int) -> dict[str, Any]:
        """POST /v2/products/{id}/orders/leverage — body ``{"leverage": "<n>"}`` (string per docs)."""
        lev = str(int(leverage)) if float(leverage).is_integer() else str(leverage)
        return (
            await self._request(
                "POST",
                f"/v2/products/{product_id}/orders/leverage",
                json_body={"leverage": lev},
                auth=True,
            )
        )["result"]

    async def place_order(
        self,
        *,
        product_id: int,
        size: int,
        side: str,
        order_type: str = "market_order",
        limit_price: float | str | None = None,
        time_in_force: str = "gtc",
        post_only: bool = False,
        reduce_only: bool = False,
        client_order_id: str | None = None,
        stop_order_type: str | None = None,
        stop_price: float | str | None = None,
        stop_trigger_method: str | None = None,
        bracket_stop_loss_price: float | str | None = None,
        bracket_stop_loss_limit_price: float | str | None = None,
        bracket_take_profit_price: float | str | None = None,
        bracket_stop_trigger_method: str | None = None,
    ) -> dict[str, Any]:
        """POST /v2/orders. Prices go as strings (Delta returns big decimals as strings and asks for
        them the same way); ``client_order_id`` is capped at 32 characters by the venue."""
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be buy/sell, got {side!r}")
        if size <= 0:
            raise ValueError("size must be a positive number of contracts")
        body: dict[str, Any] = {
            "product_id": int(product_id),
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "post_only": bool(post_only),
            "reduce_only": bool(reduce_only),
        }
        if order_type == "limit_order":
            if limit_price is None:
                raise ValueError("limit_order needs limit_price")
            body["limit_price"] = str(limit_price)
        if client_order_id:
            body["client_order_id"] = client_order_id[:32]
        if stop_order_type:
            body["stop_order_type"] = stop_order_type
            if stop_price is not None:
                body["stop_price"] = str(stop_price)
            if stop_trigger_method:
                body["stop_trigger_method"] = stop_trigger_method
        if bracket_stop_loss_price is not None:
            body["bracket_stop_loss_price"] = str(bracket_stop_loss_price)
            if bracket_stop_loss_limit_price is not None:
                body["bracket_stop_loss_limit_price"] = str(bracket_stop_loss_limit_price)
            body["bracket_stop_trigger_method"] = bracket_stop_trigger_method or "mark_price"
        if bracket_take_profit_price is not None:
            body["bracket_take_profit_price"] = str(bracket_take_profit_price)
            body.setdefault(
                "bracket_stop_trigger_method", bracket_stop_trigger_method or "mark_price"
            )
        return (await self._request("POST", "/v2/orders", json_body=body, auth=True))["result"]

    async def cancel_order(
        self, order_id: int, product_id: int, client_order_id: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"id": int(order_id), "product_id": int(product_id)}
        if client_order_id:
            body["client_order_id"] = client_order_id
        return (await self._request("DELETE", "/v2/orders", json_body=body, auth=True))["result"]

    async def cancel_all(self, product_id: int | None = None, **filters: bool) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if product_id is not None:
            body["product_id"] = int(product_id)
        body.update(filters)
        return await self._request("DELETE", "/v2/orders/all", json_body=body, auth=True)

    async def close_all_positions(self) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v2/positions/close_all",
            json_body={"close_all_portfolio": True, "close_all_isolated": True},
            auth=True,
        )
