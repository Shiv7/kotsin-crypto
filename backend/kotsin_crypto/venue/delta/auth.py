"""Delta India request signing (docs.delta.exchange → Authentication).

REST  signature = hex(HMAC_SHA256(secret, method + timestamp + path + query_string + body))
      headers: ``api-key``, ``timestamp`` (unix seconds), ``signature``, ``User-Agent`` (mandatory).
      Accepted only within 5 s of ``timestamp`` — sign immediately before sending.
      ``query_string`` includes the leading ``?`` and must be byte-identical to what goes on the wire,
      so ``rest.py`` builds the URL from the same string it signs and never lets the HTTP client
      re-encode it.

WS    {"type": "key-auth", "payload": {"api-key", "timestamp", "signature"}}
      signature = hex(HMAC_SHA256(secret, "GET" + timestamp + "/live"))
      (the old {"type": "auth"} message is deprecated.)
"""

from __future__ import annotations

import hashlib
import hmac
import time


def sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def rest_signature(
    secret: str, method: str, timestamp: int | str, path: str, query: str = "", body: str = ""
) -> str:
    return sign(secret, f"{method.upper()}{timestamp}{path}{query}{body}")


def rest_headers(
    api_key: str,
    secret: str,
    method: str,
    path: str,
    query: str = "",
    body: str = "",
    timestamp: int | None = None,
) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    return {
        "api-key": api_key,
        "timestamp": ts,
        "signature": rest_signature(secret, method, ts, path, query, body),
    }


def ws_auth_message(api_key: str, secret: str, timestamp: int | None = None) -> dict[str, object]:
    ts = int(time.time()) if timestamp is None else timestamp
    return {
        "type": "key-auth",
        "payload": {
            "api-key": api_key,
            "timestamp": ts,
            "signature": sign(secret, f"GET{ts}/live"),
        },
    }
