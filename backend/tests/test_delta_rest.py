"""The bytes we sign must be the bytes we send — verified with a mock transport."""

from __future__ import annotations

import json

import httpx
import pytest

from kotsin_crypto.config import DeltaEnv, Settings
from kotsin_crypto.venue.delta.auth import rest_signature
from kotsin_crypto.venue.delta.rest import (
    DeltaApiError,
    DeltaRateLimited,
    DeltaRest,
    RateBudget,
    weight_for,
)


def _client(handler, settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.endpoints.rest, transport=httpx.MockTransport(handler)
    )


async def test_signed_get_signs_exactly_what_is_sent() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        delta_env=DeltaEnv.TESTNET,
        delta_api_key="k",
        delta_api_secret="s",
    )
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw_path"] = request.url.raw_path.decode()
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"success": True, "result": [{"asset_symbol": "USD"}]})

    rest = DeltaRest(settings, client=_client(handler, settings))
    result = await rest._request(
        "GET", "/v2/orders", params={"product_id": 27, "state": "open"}, auth=True
    )
    assert result["result"][0]["asset_symbol"] == "USD"
    assert seen["raw_path"] == "/v2/orders?product_id=27&state=open"
    h = seen["headers"]
    assert h["api-key"] == "k"
    assert h["user-agent"].startswith("kotsin-crypto/")
    assert h["signature"] == rest_signature(
        "s", "GET", h["timestamp"], "/v2/orders", "?product_id=27&state=open"
    )


async def test_private_call_without_keys_is_refused_locally(testnet_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — must not be reached
        raise AssertionError("no request should be sent without keys")

    rest = DeltaRest(testnet_settings, client=_client(handler, testnet_settings))
    with pytest.raises(DeltaApiError, match="not configured"):
        await rest.wallet_balances()


async def test_venue_429_surfaces_reset(testnet_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-RATE-LIMIT-RESET": "1234"}, json={"success": False})

    rest = DeltaRest(testnet_settings, client=_client(handler, testnet_settings))
    with pytest.raises(DeltaRateLimited) as exc:
        await rest.ticker("BTCUSD")
    assert exc.value.reset_ms == 1234 and exc.value.local is False


async def test_api_error_payload_is_raised(testnet_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"success": False, "error": {"code": "invalid_symbol"}})

    rest = DeltaRest(testnet_settings, client=_client(handler, testnet_settings))
    with pytest.raises(DeltaApiError, match="invalid_symbol"):
        await rest.ticker("NOPE")


async def test_all_products_follows_cursor(testnet_settings: Settings) -> None:
    pages = {
        None: {"success": True, "result": [{"id": 1}], "meta": {"after": "c1"}},
        "c1": {"success": True, "result": [{"id": 2}], "meta": {"after": None}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        return httpx.Response(200, json=pages[after])

    rest = DeltaRest(testnet_settings, client=_client(handler, testnet_settings))
    assert [p["id"] for p in await rest.all_products()] == [1, 2]


def test_weights_follow_the_documented_table() -> None:
    assert weight_for("GET", "/v2/products") == 3
    assert weight_for("POST", "/v2/orders") == 5
    assert weight_for("GET", "/v2/orders/history") == 10
    assert weight_for("GET", "/v2/fills") == 10
    assert weight_for("POST", "/v2/orders/batch") == 25


def test_local_budget_refuses_before_the_venue_would() -> None:
    b = RateBudget(quota=10, window_s=300)
    b.charge(5)
    b.charge(5)
    with pytest.raises(DeltaRateLimited) as exc:
        b.charge(3)
    assert exc.value.local is True
    assert b.remaining() == 0


def test_body_is_compact_json() -> None:
    assert json.dumps({"a": 1, "b": [1, 2]}, separators=(",", ":")) == '{"a":1,"b":[1,2]}'
