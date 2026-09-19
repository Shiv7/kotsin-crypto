"""Typed, closed configuration.

Rule R1 (docs/LEARNINGS.md): a config key that nothing reads is a bug that hides. So:

* every key is a declared field here — there is no other place a setting can come from;
* unknown keys in the ``.env`` file fail validation (``extra="forbid"``);
* unknown ``KC_*`` variables in the process environment fail at boot (``assert_no_unknown_env``).

Trading mode (SHADOW / PAPER / LIVE_CAPPED / LIVE) is deliberately *not* here — it is state in the
control table (R9), so a restart cannot silently change it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "KC_"


class DeltaEnv(StrEnum):
    TESTNET = "testnet"
    MAINNET = "mainnet"


class DeltaEndpoints(BaseModel, frozen=True):
    rest: str
    ws_public: str
    ws_private: str


# Verified 2026-09-20 against docs.delta.exchange (India site). See docs/DELTA_INDIA_FACTS.md.
ENDPOINTS: dict[DeltaEnv, DeltaEndpoints] = {
    DeltaEnv.MAINNET: DeltaEndpoints(
        rest="https://api.india.delta.exchange",
        ws_public="wss://public-socket.india.delta.exchange",
        ws_private="wss://socket.india.delta.exchange",
    ),
    DeltaEnv.TESTNET: DeltaEndpoints(
        rest="https://cdn-ind.testnet.deltaex.org",
        ws_public="wss://socket-ind-pub.testnet.deltaex.org",
        ws_private="wss://socket-ind.testnet.deltaex.org",
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    env: str = "dev"
    log_level: str = "INFO"

    delta_env: DeltaEnv = DeltaEnv.TESTNET
    delta_api_key: SecretStr | None = None
    delta_api_secret: SecretStr | None = None

    symbols: str = "BTCUSD,ETHUSD,SOLUSD"

    data_dir: Path = Path("./data")
    db_url: str = "sqlite+aiosqlite:///./data/kotsin_crypto.db"

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    api_host: str = "127.0.0.1"
    api_port: int = 8400

    engine_enabled: bool = True  # false → API only (tests, UI work without a feed)
    paper_initial_usd: float = 10_000.0  # dummy wallet per strategy in PAPER mode
    backfill_hours: float = 6.0  # 1m history seeded from REST at boot

    @field_validator("symbols")
    @classmethod
    def _symbols_nonempty(cls, v: str) -> str:
        if not [s for s in v.split(",") if s.strip()]:
            raise ValueError("KC_SYMBOLS must list at least one symbol")
        return v

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]

    @property
    def endpoints(self) -> DeltaEndpoints:
        return ENDPOINTS[self.delta_env]

    @property
    def has_api_keys(self) -> bool:
        return self.delta_api_key is not None and self.delta_api_secret is not None


class UnknownConfigKeys(RuntimeError):
    """Raised at boot when the environment carries a KC_* variable no field declares."""


def known_env_keys() -> set[str]:
    return {ENV_PREFIX + name.upper() for name in Settings.model_fields}


def assert_no_unknown_env(environ: Mapping[str, str] | None = None) -> None:
    """Fail if the process environment carries a ``KC_*`` variable that no field declares.

    pydantic-settings validates unknown keys in the ``.env`` file but deliberately ignores unknown
    *process* environment variables, because the environment is shared with other programs. For a
    prefixed namespace that is the wrong trade-off: ``KC_DELTA_ENVIRONMENT=mainnet`` (typo) must not
    silently run on testnet.
    """
    env = os.environ if environ is None else environ
    known = known_env_keys()
    unknown = sorted(k for k in env if k.upper().startswith(ENV_PREFIX) and k.upper() not in known)
    if unknown:
        raise UnknownConfigKeys(
            f"unknown config keys: {', '.join(unknown)} — known keys: {', '.join(sorted(known))}"
        )
