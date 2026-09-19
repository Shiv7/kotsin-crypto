"""LEARNINGS R1: config is typed and closed."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kotsin_crypto.config import (
    ENDPOINTS,
    DeltaEnv,
    Settings,
    UnknownConfigKeys,
    assert_no_unknown_env,
    known_env_keys,
)


def test_defaults_are_testnet_and_three_symbols() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.delta_env is DeltaEnv.TESTNET
    assert s.symbol_list == ["BTCUSD", "ETHUSD", "SOLUSD"]
    assert s.endpoints.rest == "https://cdn-ind.testnet.deltaex.org"
    assert not s.has_api_keys


def test_mainnet_endpoints_are_the_india_hosts() -> None:
    e = ENDPOINTS[DeltaEnv.MAINNET]
    assert e.rest == "https://api.india.delta.exchange"
    assert e.ws_public == "wss://public-socket.india.delta.exchange"
    assert e.ws_private == "wss://socket.india.delta.exchange"


def test_unknown_key_in_dotenv_fails(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("KC_DELTA_ENV=mainnet\nKC_DELTA_ENVIRONMENT=mainnet\n")  # second one is a typo
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        Settings(_env_file=env)  # type: ignore[call-arg]


def test_unknown_key_in_process_env_fails() -> None:
    with pytest.raises(UnknownConfigKeys, match="KC_TYPO"):
        assert_no_unknown_env({"KC_DELTA_ENV": "testnet", "KC_TYPO": "1", "HOME": "/x"})


def test_known_keys_pass_and_are_case_insensitive() -> None:
    assert_no_unknown_env({"kc_delta_env": "testnet", "KC_API_PORT": "8400"})


def test_every_documented_key_is_a_field() -> None:
    """.env.example must not advertise a key that config.py does not read."""
    example = Path(__file__).resolve().parents[2] / ".env.example"
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert documented == known_env_keys()


def test_symbols_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, symbols=" , ")  # type: ignore[call-arg]


def test_bad_enum_fails_loudly() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, delta_env="mainet")  # type: ignore[call-arg]
