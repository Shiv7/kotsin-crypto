"""Signature vectors computed independently with hmac/hashlib on 2026-09-20."""

from __future__ import annotations

from kotsin_crypto.venue.delta.auth import rest_headers, rest_signature, ws_auth_message

SECRET = "test-secret"
TS = 1700000000


def test_rest_signature_get_no_query() -> None:
    assert (
        rest_signature(SECRET, "GET", TS, "/v2/wallet/balances")
        == "63f4e0ed83545296429f440440e63e7be52c52ff86b73649249eb6466ab3db00"
    )


def test_rest_signature_get_with_query_includes_question_mark() -> None:
    assert (
        rest_signature(SECRET, "GET", TS, "/v2/orders", "?product_id=27&state=open")
        == "5a6ec50151b62b37af4cec407e0d26dcce3944b491330db96130a7be4af292ae"
    )


def test_rest_signature_post_with_body() -> None:
    assert (
        rest_signature(SECRET, "POST", TS, "/v2/orders", "", '{"product_id":27,"size":1}')
        == "ab25d12525aa9e5d38519550d093dcd3693d8bfdac7bd371e9ae324eb2474b00"
    )


def test_rest_headers_shape() -> None:
    h = rest_headers("key", SECRET, "GET", "/v2/wallet/balances", timestamp=TS)
    assert h == {
        "api-key": "key",
        "timestamp": str(TS),
        "signature": "63f4e0ed83545296429f440440e63e7be52c52ff86b73649249eb6466ab3db00",
    }


def test_ws_auth_message_uses_key_auth_and_live_path() -> None:
    msg = ws_auth_message("key", SECRET, timestamp=TS)
    assert msg["type"] == "key-auth"
    assert msg["payload"] == {
        "api-key": "key",
        "timestamp": TS,
        "signature": "18b3e4c6f67a3678e0e31a655eab20c9795208903e00cf68ab674869d8f39ed3",
    }
