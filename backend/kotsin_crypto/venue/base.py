"""Venue surface. Delta is the first implementation; a second venue (or a ccxt shim for data) must fit
this surface without touching strategy / risk code. Kept deliberately small; it grows with the steps."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MarketData(Protocol):
    async def all_products(self, **filters: Any) -> list[dict[str, Any]]: ...
    async def candles(
        self, symbol: str, resolution: str, start: int, end: int
    ) -> list[dict[str, Any]]: ...
    async def orderbook(self, symbol: str, depth: int = 25) -> dict[str, Any]: ...


@runtime_checkable
class AccountRead(Protocol):
    async def wallet_balances(self) -> list[dict[str, Any]]: ...
    async def positions(self) -> list[dict[str, Any]]: ...
