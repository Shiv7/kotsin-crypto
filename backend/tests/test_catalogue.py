from __future__ import annotations

from decimal import Decimal

from kotsin_crypto.venue.delta.catalogue import Catalogue, Product

BTC_RAW = {
    "id": 27,
    "symbol": "BTCUSD",
    "contract_type": "perpetual_futures",
    "state": "live",
    "tick_size": "0.500000000000000000",
    "contract_value": "0.001000000000000000",
    "contract_unit_currency": "BTC",
    "settling_asset": {"symbol": "USD"},
    "underlying_asset": {"symbol": "BTC"},
    "initial_margin": "0.5",
    "maintenance_margin": "0.25",
    "default_leverage": "200.000000000000000000",
    "maker_commission_rate": "0.0002",
    "taker_commission_rate": "0.0005",
    "position_size_limit": 125000,
}


def test_product_from_api_parses_decimals() -> None:
    p = Product.from_api(BTC_RAW)
    assert p.id == 27 and p.symbol == "BTCUSD"
    assert p.tick_size == Decimal("0.5")
    assert p.contract_value == Decimal("0.001")
    assert p.default_leverage == Decimal(200)
    assert p.taker_fee == Decimal("0.0005")
    assert p.settling_asset == "USD" and p.underlying == "BTC"


def test_round_price_snaps_to_tick() -> None:
    p = Product.from_api(BTC_RAW)
    assert p.round_price(Decimal("81408.26")) == Decimal("81408.5")
    assert p.round_price(Decimal("81408.24")) == Decimal("81408.0")


def test_notional_is_contracts_times_value_times_price() -> None:
    p = Product.from_api(BTC_RAW)
    assert p.notional(3, Decimal("80000")) == Decimal("240.000")


def test_catalogue_lookups() -> None:
    c = Catalogue([Product.from_api(BTC_RAW)])
    assert len(c) == 1 and "BTCUSD" in c
    assert c.by_id(27).symbol == "BTCUSD"
    assert c.symbols() == ["BTCUSD"]
