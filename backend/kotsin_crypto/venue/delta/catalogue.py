"""Product catalogue: the per-contract numbers everything downstream needs (tick, contract value,
margins, fees, leverage). Loaded once at boot, refreshed on ``product_updates``."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel

from .rest import DeltaRest


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


class Product(BaseModel, frozen=True):
    id: int
    symbol: str
    contract_type: str
    state: str
    tick_size: Decimal
    contract_value: Decimal
    contract_unit_currency: str | None = None
    settling_asset: str | None = None
    underlying: str | None = None
    initial_margin_pct: Decimal | None = None
    maintenance_margin_pct: Decimal | None = None
    default_leverage: Decimal | None = None
    maker_fee: Decimal | None = None
    taker_fee: Decimal | None = None
    position_size_limit: int | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Product:
        return cls(
            id=int(raw["id"]),
            symbol=raw["symbol"],
            contract_type=raw["contract_type"],
            state=raw.get("state", "unknown"),
            tick_size=Decimal(str(raw["tick_size"])),
            contract_value=Decimal(str(raw["contract_value"])),
            contract_unit_currency=raw.get("contract_unit_currency"),
            settling_asset=(raw.get("settling_asset") or {}).get("symbol"),
            underlying=(raw.get("underlying_asset") or {}).get("symbol"),
            initial_margin_pct=_dec(raw.get("initial_margin")),
            maintenance_margin_pct=_dec(raw.get("maintenance_margin")),
            default_leverage=_dec(raw.get("default_leverage")),
            maker_fee=_dec(raw.get("maker_commission_rate")),
            taker_fee=_dec(raw.get("taker_commission_rate")),
            position_size_limit=raw.get("position_size_limit"),
        )

    def round_price(self, price: Decimal) -> Decimal:
        return (price / self.tick_size).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        ) * self.tick_size

    def notional(self, contracts: int, price: Decimal) -> Decimal:
        """Notional in the settling asset for ``contracts`` contracts at ``price``."""
        return Decimal(contracts) * self.contract_value * price


class Catalogue:
    def __init__(self, products: Iterable[Product]) -> None:
        self._by_symbol = {p.symbol: p for p in products}
        self._by_id = {p.id: p for p in self._by_symbol.values()}

    @classmethod
    async def load(
        cls, rest: DeltaRest, *, contract_types: str = "perpetual_futures", states: str = "live"
    ) -> Catalogue:
        raw = await rest.all_products(contract_types=contract_types, states=states)
        return cls(Product.from_api(r) for r in raw)

    def __len__(self) -> int:
        return len(self._by_symbol)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._by_symbol

    def by_symbol(self, symbol: str) -> Product:
        return self._by_symbol[symbol]

    def by_id(self, product_id: int) -> Product:
        return self._by_id[product_id]

    def symbols(self) -> list[str]:
        return sorted(self._by_symbol)
