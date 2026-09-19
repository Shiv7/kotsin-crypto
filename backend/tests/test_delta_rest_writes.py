"""Private writes: the JSON body Delta receives and the signature over exactly those bytes — verified
with a mock transport, never against the venue."""

from __future__ import annotations

import json

import httpx
import pytest

from kotsin_crypto.config import DeltaEnv, Settings
from kotsin_crypto.venue.delta.auth import rest_signature
from kotsin_crypto.venue.delta.rest import DeltaApiError, DeltaRest


def _settings(**kw) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        delta_env=DeltaEnv.TESTNET,
        delta_api_key="k",
        delta_api_secret="s",
        engine_enabled=False,
        **kw,
    )


class Capture:
    def __init__(
        self, result: object = None, status: int = 200, payload: dict | None = None
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.result = result
        self.status = status
        self.payload = payload

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.payload is not None:
            return httpx.Response(self.status, json=self.payload)
        return httpx.Response(self.status, json={"success": True, "result": self.result})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self) -> dict:
        return json.loads(self.last.content.decode())


def _rest(cap: Capture, settings: Settings | None = None) -> DeltaRest:
    settings = settings or _settings()
    client = httpx.AsyncClient(base_url=settings.endpoints.rest, transport=httpx.MockTransport(cap))
    return DeltaRest(settings, client=client)


def _assert_signed(cap: Capture, method: str, path: str) -> None:
    h = cap.last.headers
    body = cap.last.content.decode()
    assert h["signature"] == rest_signature("s", method, h["timestamp"], path, "", body)
    assert h["api-key"] == "k"


async def test_market_entry_with_bracket_stop_body_and_signature() -> None:
    cap = Capture(result={"id": 1, "state": "open"})
    rest = _rest(cap)
    await rest.place_order(
        product_id=27,
        size=1,
        side="buy",
        client_order_id="CAN2-BTCUSD-1700000000-L",
        bracket_stop_loss_price=81_400.5,
    )
    assert cap.last.method == "POST" and cap.last.url.path == "/v2/orders"
    body = cap.body()
    assert body == {
        "product_id": 27,
        "size": 1,
        "side": "buy",
        "order_type": "market_order",
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": False,
        "client_order_id": "CAN2-BTCUSD-1700000000-L",
        "bracket_stop_loss_price": "81400.5",
        "bracket_stop_trigger_method": "mark_price",
    }
    assert isinstance(body["bracket_stop_loss_price"], str)  # Delta wants prices as strings
    assert cap.last.headers["content-type"] == "application/json"
    _assert_signed(cap, "POST", "/v2/orders")


async def test_reduce_only_exit_and_client_order_id_is_capped_at_32() -> None:
    cap = Capture(result={"id": 2})
    rest = _rest(cap)
    await rest.place_order(
        product_id=27, size=1, side="sell", reduce_only=True, client_order_id="x" * 40
    )
    body = cap.body()
    assert body["reduce_only"] is True and "bracket_stop_loss_price" not in body
    assert body["client_order_id"] == "x" * 32


async def test_place_order_validates_locally_before_any_request() -> None:
    cap = Capture(result={})
    rest = _rest(cap)
    with pytest.raises(ValueError, match="side"):
        await rest.place_order(product_id=27, size=1, side="long")
    with pytest.raises(ValueError, match="size"):
        await rest.place_order(product_id=27, size=0, side="buy")
    with pytest.raises(ValueError, match="limit_price"):
        await rest.place_order(product_id=27, size=1, side="buy", order_type="limit_order")
    assert cap.requests == []


async def test_set_leverage_sends_string_and_get_leverage_path() -> None:
    cap = Capture(result={"leverage": "5", "product_id": 27})
    rest = _rest(cap)
    await rest.set_leverage(27, 5.0)
    assert cap.last.method == "POST"
    assert cap.last.url.path == "/v2/products/27/orders/leverage"
    assert cap.body() == {"leverage": "5"}
    _assert_signed(cap, "POST", "/v2/products/27/orders/leverage")
    got = await rest.get_leverage(27)
    assert cap.last.method == "GET" and got["leverage"] == "5"


async def test_cancel_order_uses_delete_with_json_body() -> None:
    cap = Capture(result={"id": 5, "state": "cancelled"})
    rest = _rest(cap)
    await rest.cancel_order(5, 27, "abc")
    assert cap.last.method == "DELETE" and cap.last.url.path == "/v2/orders"
    assert cap.body() == {"id": 5, "product_id": 27, "client_order_id": "abc"}
    _assert_signed(cap, "DELETE", "/v2/orders")


async def test_cancel_all_and_close_all_bodies() -> None:
    cap = Capture(result={})
    rest = _rest(cap)
    await rest.cancel_all(27, cancel_limit_orders=True)
    assert cap.last.method == "DELETE" and cap.last.url.path == "/v2/orders/all"
    assert cap.body() == {"product_id": 27, "cancel_limit_orders": True}
    await rest.close_all_positions()
    assert cap.last.method == "POST" and cap.last.url.path == "/v2/positions/close_all"
    assert cap.body() == {"close_all_portfolio": True, "close_all_isolated": True}
    _assert_signed(cap, "POST", "/v2/positions/close_all")


async def test_private_reads_paths_and_query() -> None:
    cap = Capture(result=[])
    rest = _rest(cap)
    await rest.open_orders(product_ids="27")
    assert cap.last.url.raw_path.decode() == "/v2/orders?product_ids=27&states=open%2Cpending"
    await rest.get_order(9)
    assert cap.last.url.path == "/v2/orders/9"
    await rest.get_order_by_client_id("cid-1")
    assert cap.last.url.path == "/v2/orders/client_order_id/cid-1"
    await rest.positions()
    assert cap.last.url.path == "/v2/positions/margined"
    await rest.fills(page_size=500)
    assert cap.last.url.raw_path.decode() == "/v2/fills?page_size=50"


async def test_ip_not_whitelisted_surfaces_client_ip() -> None:
    cap = Capture(
        status=401,
        payload={
            "success": False,
            "error": {
                "code": "ip_not_whitelisted_for_api_key",
                "context": {"client_ip": "203.0.113.7"},
            },
        },
    )
    rest = _rest(cap)
    with pytest.raises(DeltaApiError) as exc:
        await rest.wallet_balances()
    assert exc.value.code == "ip_not_whitelisted_for_api_key"
    assert exc.value.context == {"client_ip": "203.0.113.7"}
    assert "client_ip=203.0.113.7" in str(exc.value) and "whitelist" in str(exc.value)


async def test_default_client_pins_ipv4_when_configured() -> None:
    pinned = DeltaRest(_settings(delta_force_ipv4=True))
    free = DeltaRest(_settings(delta_force_ipv4=False))
    try:
        pool = pinned._client._transport._pool  # type: ignore[attr-defined]
        assert getattr(pool, "_local_address", None) == "0.0.0.0"
        pool_free = free._client._transport._pool  # type: ignore[attr-defined]
        assert getattr(pool_free, "_local_address", None) is None
    finally:
        await pinned.aclose()
        await free.aclose()
