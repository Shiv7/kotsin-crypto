"""Hits Delta India's public API (no key). Run with: uv run pytest -m live"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from kotsin_crypto.config import Settings
from kotsin_crypto.venue.delta.catalogue import Catalogue
from kotsin_crypto.venue.delta.models import Orderbook
from kotsin_crypto.venue.delta.rest import DeltaRest

pytestmark = pytest.mark.live


async def test_mainnet_catalogue_has_the_v1_universe(mainnet_settings: Settings) -> None:
    rest = DeltaRest(mainnet_settings)
    try:
        cat = await Catalogue.load(rest)
    finally:
        await rest.aclose()
    for sym in mainnet_settings.symbol_list:
        assert sym in cat, f"{sym} missing from live perpetuals"
    btc = cat.by_symbol("BTCUSD")
    assert btc.id == 27
    assert btc.contract_value == Decimal("0.001")
    assert btc.tick_size == Decimal("0.5")
    assert (
        btc.default_leverage is not None and btc.default_leverage >= 100
    )  # why exec/live.py sets it first


async def test_mainnet_orderbook_and_candles(mainnet_settings: Settings) -> None:
    rest = DeltaRest(mainnet_settings)
    try:
        ob = Orderbook.from_api(await rest.orderbook("BTCUSD", depth=5))
        assert ob.best_bid and ob.best_ask and ob.best_ask.price > ob.best_bid.price
        end = int(time.time())
        candles = await rest.candles("BTCUSD", "5m", end - 3600, end)
        assert 10 <= len(candles) <= 13
        assert {"open", "high", "low", "close", "volume", "time"} <= set(candles[0])
        used = rest.budget.used()  # orderbook + candles, each weight 3
        assert used == 6
    finally:
        await rest.aclose()
