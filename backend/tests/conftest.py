from __future__ import annotations

import pytest

from kotsin_crypto.config import DeltaEnv, Settings


@pytest.fixture
def testnet_settings() -> Settings:
    return Settings(_env_file=None, delta_env=DeltaEnv.TESTNET, engine_enabled=False)  # type: ignore[call-arg]


@pytest.fixture
def mainnet_settings() -> Settings:
    return Settings(_env_file=None, delta_env=DeltaEnv.MAINNET, engine_enabled=False)  # type: ignore[call-arg]
