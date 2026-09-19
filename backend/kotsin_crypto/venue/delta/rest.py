"""Signed REST client for Delta India with a local rate-limit budget.

Public endpoints need no key. Private *reads* (balances, positions) are here from step 1 so the
testnet key can be smoke-tested; private *writes* (orders, leverage, margin) arrive with ``exec/live.py``
in step 8 — nothing in this module can move money yet.

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
    def __init__(self, status: int, error: Any) -> None:
        super().__init__(f"delta api error {status}: {error}")
        self.status = status
        self.error = error


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
        self._client = client or httpx.AsyncClient(
            base_url=settings.endpoints.rest, timeout=httpx.Timeout(15.0, connect=5.0)
        )
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

    # ---- private reads (writes come with exec/live.py, step 8) --------------------------------

    async def wallet_balances(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/v2/wallet/balances", auth=True))["result"]

    async def positions(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/v2/positions/margined", auth=True))["result"]
