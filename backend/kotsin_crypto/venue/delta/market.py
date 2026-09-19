"""Read-only market views over Delta's public REST: the perpetuals universe (funding, basis, OI) and
option chains (greeks, IV, OI, max pain, put/call ratio). Cached briefly so the UI can poll freely
inside the rate budget."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .rest import DeltaRest

FUNDING_INTERVALS_PER_YEAR = 3 * 365


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def normalize_perp(t: dict[str, Any]) -> dict[str, Any]:
    mark, spot = _f(t.get("mark_price")), _f(t.get("spot_price"))
    fr = _f(t.get("funding_rate"))
    q = t.get("quotes") or {}
    pb = t.get("price_band") or {}
    return {
        "symbol": t.get("symbol"),
        "product_id": t.get("product_id"),
        "mark": mark,
        "spot": spot,
        "last": _f(t.get("close")),
        "change_24h_pct": _f(t.get("mark_change_24h")),
        "funding_pct": fr,
        "funding_annualized_pct": fr * FUNDING_INTERVALS_PER_YEAR if fr is not None else None,
        "basis_bps": (mark - spot) / spot * 1e4 if mark and spot else None,
        "oi_contracts": _f(t.get("oi_contracts")),
        "oi_usd": _f(t.get("oi_value_usd")),
        "oi_change_usd_6h": _f(t.get("oi_change_usd_6h")),
        "turnover_usd": _f(t.get("turnover_usd")),
        "volume_contracts": _f(t.get("volume")),
        "bid": _f(q.get("best_bid")),
        "ask": _f(q.get("best_ask")),
        "band_lo": _f(pb.get("lower_limit")),
        "band_hi": _f(pb.get("upper_limit")),
        "leverage": t.get("leverage"),
        "reduce_only": bool(t.get("oi_reduce_only_mode")),
        "status": t.get("product_trading_status"),
        "tags": t.get("tags") or [],
    }


def normalize_option(t: dict[str, Any]) -> dict[str, Any]:
    q = t.get("quotes") or {}
    g = t.get("greeks") or {}
    return {
        "symbol": t.get("symbol"),
        "product_id": t.get("product_id"),
        "type": "C" if t.get("contract_type") == "call_options" else "P",
        "strike": _f(t.get("strike_price")),
        "mark": _f(t.get("mark_price")),
        "bid": _f(q.get("best_bid")),
        "ask": _f(q.get("best_ask")),
        "bid_size": _f(q.get("bid_size")),
        "ask_size": _f(q.get("ask_size")),
        "iv": _f(q.get("mark_iv")),
        "bid_iv": _f(q.get("bid_iv")),
        "ask_iv": _f(q.get("ask_iv")),
        "delta": _f(g.get("delta")),
        "gamma": _f(g.get("gamma")),
        "theta": _f(g.get("theta")),
        "vega": _f(g.get("vega")),
        "rho": _f(g.get("rho")),
        "oi_contracts": _f(t.get("oi_contracts")),
        "oi_usd": _f(t.get("oi_value_usd")),
        "volume": _f(t.get("volume")),
        "turnover_usd": _f(t.get("turnover_usd")),
        "spot": _f(t.get("spot_price")),
    }


def chain_summary(rows: list[dict[str, Any]], spot: float | None) -> dict[str, Any]:
    strikes = sorted({r["strike"] for r in rows if r["strike"] is not None})
    calls = [r for r in rows if r["type"] == "C"]
    puts = [r for r in rows if r["type"] == "P"]
    call_oi = sum(r["oi_contracts"] or 0.0 for r in calls)
    put_oi = sum(r["oi_contracts"] or 0.0 for r in puts)
    max_pain = None
    if strikes:
        best = None
        for s in strikes:
            pain = sum(
                (r["oi_contracts"] or 0.0) * max(0.0, s - r["strike"])
                for r in calls
                if r["strike"] is not None
            )
            pain += sum(
                (r["oi_contracts"] or 0.0) * max(0.0, r["strike"] - s)
                for r in puts
                if r["strike"] is not None
            )
            if best is None or pain < best[0]:
                best = (pain, s)
        max_pain = best[1] if best else None
    atm_iv = None
    if spot and strikes:
        k = min(strikes, key=lambda x: abs(x - spot))
        ivs = [r["iv"] for r in rows if r["strike"] == k and r["iv"] is not None]
        atm_iv = sum(ivs) / len(ivs) if ivs else None
    return {
        "spot": spot,
        "strikes": len(strikes),
        "call_oi_contracts": call_oi,
        "put_oi_contracts": put_oi,
        "pcr_oi": put_oi / call_oi if call_oi else None,
        "max_pain": max_pain,
        "atm_iv": atm_iv,
        "total_turnover_usd": sum(r["turnover_usd"] or 0.0 for r in rows),
    }


class MarketData:
    def __init__(self, rest: DeltaRest) -> None:
        self.rest = rest
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _cached(self, key: str, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> Any:
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        data = await fetch()
        self._cache[key] = (now, data)
        return data

    async def perps(self, ttl: float = 30.0) -> list[dict[str, Any]]:
        async def fetch() -> list[dict[str, Any]]:
            raw = await self.rest.tickers("perpetual_futures")
            rows = [normalize_perp(t) for t in raw]
            rows.sort(key=lambda r: -(r["turnover_usd"] or 0.0))
            return rows

        return await self._cached("perps", ttl, fetch)

    async def option_expiries(self, underlying: str, ttl: float = 300.0) -> list[dict[str, Any]]:
        async def fetch() -> list[dict[str, Any]]:
            products = await self.rest.all_products(
                contract_types="call_options,put_options",
                states="live",
                underlying_asset_symbols=underlying,
            )
            by_day: dict[str, dict[str, Any]] = {}
            for p in products:
                st = p.get("settlement_time") or ""
                day = st[:10]
                if not day:
                    continue
                d = by_day.setdefault(
                    day, {"expiry": day, "settlement_time": st, "contracts": 0, "strikes": set()}
                )
                d["contracts"] += 1
                if p.get("strike_price") is not None:
                    d["strikes"].add(float(p["strike_price"]))
            out = []
            for d in sorted(by_day.values(), key=lambda x: x["expiry"]):
                d["strikes"] = len(d["strikes"])
                out.append(d)
            return out

        return await self._cached(f"expiries:{underlying}", ttl, fetch)

    async def option_chain(self, underlying: str, expiry: str, ttl: float = 15.0) -> dict[str, Any]:
        """``expiry`` is YYYY-MM-DD; Delta wants DD-MM-YYYY."""

        async def fetch() -> dict[str, Any]:
            d = datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=UTC)
            raw = await self.rest.tickers(
                "call_options,put_options",
                underlying_asset_symbols=underlying,
                expiry_date=d.strftime("%d-%m-%Y"),
            )
            rows = sorted(
                (normalize_option(t) for t in raw), key=lambda r: (r["strike"] or 0.0, r["type"])
            )
            spot = next((r["spot"] for r in rows if r["spot"]), None)
            return {
                "underlying": underlying,
                "expiry": expiry,
                "fetched_ts": time.time(),
                "summary": chain_summary(rows, spot),
                "rows": rows,
            }

        return await self._cached(f"chain:{underlying}:{expiry}", ttl, fetch)
