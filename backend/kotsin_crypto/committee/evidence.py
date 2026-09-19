"""The evidence pack: a compact, point-in-time, numeric view of one symbol that the committee reasons
over. Trading-R1's first imperative is input quality — the model conditions on curated numbers with
stable keys it can cite, never on raw feeds. No news or social sources are wired (no keys, and they
were the look-ahead trap in both papers)."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..engine import Engine


def _r(v: float | None, d: int = 4) -> float | None:
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(v, d)


async def build_pack(engine: Engine, symbol: str) -> dict[str, Any]:
    now = time.time()
    b5 = engine.store.bars(symbol, "5m", 12)
    b1h = engine.store.bars(symbol, "1h", 24)
    last = engine.marks.get(symbol) or engine.last_price.get(symbol)
    pack: dict[str, Any] = {"symbol": symbol, "as_of": now, "px.last": _r(last, 2)}

    def ret(bars: list[Any], n: int) -> float | None:
        if len(bars) < n + 1 or not bars[-1].close or not bars[-1 - n].close:
            return None
        return _r((bars[-1].close / bars[-1 - n].close - 1) * 100, 3)

    pack["px.ret_1h_pct"] = ret(b5, 12) if len(b5) >= 13 else ret(b1h, 1)
    pack["px.ret_4h_pct"] = ret(b1h, 4)
    pack["px.ret_24h_pct"] = ret(b1h, 24) if len(b1h) >= 25 else None
    if b1h:
        hi, lo = max(b.high for b in b1h), min(b.low for b in b1h)
        pack["px.range_24h_pct"] = _r((hi - lo) / lo * 100, 3) if lo else None
        pack["px.pos_in_range_24h"] = _r((last - lo) / (hi - lo), 3) if last and hi > lo else None
        rets = [
            math.log(b1h[i].close / b1h[i - 1].close)
            for i in range(1, len(b1h))
            if b1h[i - 1].close
        ]
        if len(rets) >= 6:
            mean = sum(rets) / len(rets)
            pack["vol.rv_1h_bps"] = _r(
                math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * 1e4, 1
            )
    pack["bars_5m"] = [
        {
            "t": b.ts,
            "o": _r(b.open, 2),
            "h": _r(b.high, 2),
            "l": _r(b.low, 2),
            "c": _r(b.close, 2),
            "v": _r(b.volume, 0),
            "buy_ratio": _r(b.buy_volume / b.volume, 2) if b.has_micro and b.volume else None,
        }
        for b in b5
    ]

    mv = engine.micro_view(symbol, levels=5)
    q, mi, roll = mv["quotes"], mv.get("micro") or {}, mv["rolling"]
    pack.update(
        {
            "micro.spread_bps": _r(q.get("spread_bps"), 3),
            "micro.depth_imbalance_top5": _r(q.get("imbalance5"), 3),
            "micro.ofi_5m": _r(roll["5m"]["ofi"], 0),
            "micro.ofi_15m": _r(roll["15m"]["ofi"], 0),
            "micro.buy_ratio_5m": _r(roll["5m"]["buy_ratio"], 3),
            "micro.buy_ratio_15m": _r(roll["15m"]["buy_ratio"], 3),
            "micro.kyle_lambda_bps_per_1k": _r(mi.get("kyle_lambda_15m_bps_per_1k"), 4),
            "micro.vpin_fast": _r(mi.get("vpin_fast"), 3),
            "micro.vpin_daily": _r(mi.get("vpin"), 3),
            "micro.realized_vol_1m_bps": _r(mi.get("realized_vol_bps"), 1),
            "micro.trade_intensity_per_s": _r(mi.get("trade_intensity"), 3),
            "micro.large_trade_share": _r(mi.get("large_trade_share"), 3),
        }
    )
    f = engine.funding.get(symbol)
    pack["deriv.funding_pct_8h"] = _r(f.rate_pct, 5) if f else None
    pack["deriv.funding_annualized_pct"] = _r(f.rate_pct * 3 * 365, 2) if f else None
    pack["deriv.next_funding_in_min"] = _r((f.next_ts_us / 1e6 - now) / 60, 0) if f else None
    spot = engine.spot.get(symbol)
    pack["deriv.basis_bps"] = _r((last - spot) / spot * 1e4, 2) if last and spot else None
    pack["deriv.oi_contracts"] = _r(engine.oi.get(symbol), 0)
    try:
        perps = {r["symbol"]: r for r in await engine.market.perps()}
        me = perps.get(symbol) or {}
        pack["deriv.oi_usd"] = _r(me.get("oi_usd"), 0)
        pack["deriv.oi_change_6h_usd"] = _r(me.get("oi_change_usd_6h"), 0)
        pack["deriv.turnover_24h_usd"] = _r(me.get("turnover_usd"), 0)
        pack["ctx.btc_24h_pct"] = _r((perps.get("BTCUSD") or {}).get("change_24h_pct"), 2)
        pack["ctx.eth_24h_pct"] = _r((perps.get("ETHUSD") or {}).get("change_24h_pct"), 2)
        ranked = sorted(
            (r for r in perps.values() if r.get("funding_annualized_pct") is not None),
            key=lambda r: -abs(r["funding_annualized_pct"]),
        )[:3]
        pack["ctx.funding_extremes"] = [
            f"{r['symbol']} {r['funding_annualized_pct']:+.0f}%/yr" for r in ranked
        ]
    except Exception as exc:  # market context is optional
        pack["ctx.error"] = str(exc)[:80]
    underlying = symbol.replace("USD", "")
    try:
        exps = await engine.market.option_expiries(underlying)
        if exps:
            chain = await engine.market.option_chain(underlying, exps[0]["expiry"])
            s = chain["summary"]
            pack.update(
                {
                    "opt.expiry": exps[0]["expiry"],
                    "opt.atm_iv": _r(s.get("atm_iv"), 3),
                    "opt.pcr_oi": _r(s.get("pcr_oi"), 2),
                    "opt.max_pain": _r(s.get("max_pain"), 0),
                    "opt.max_pain_vs_spot_pct": _r((s["max_pain"] - last) / last * 100, 2)
                    if s.get("max_pain") and last
                    else None,
                }
            )
    except Exception as exc:
        pack["opt.error"] = str(exc)[:80]
    w = next(iter(engine.wallets.values()), None)
    pack["book.wallet_balance"] = _r(w.balance, 2) if w else None
    pack["book.wallet_day_pnl_pct"] = _r(w.day_pnl_pct, 3) if w else None
    pack["book.open_positions"] = [
        {
            "symbol": p.symbol,
            "side": p.side.value,
            "contracts": p.contracts,
            "entry": p.entry,
            "r_now": _r(p.r_now(engine.marks.get(p.symbol, p.entry)), 2),
        }
        for p in engine.positions.values()
    ]
    pack["book.recent_signals"] = [
        {"t": s["ts"], "side": s["side"], "decision": s["decision"]}
        for s in list(engine.recent_signals)[:5]
        if s["symbol"] == symbol
    ]
    return pack


def render(pack: dict[str, Any]) -> str:
    lines = [
        f"symbol: {pack['symbol']}",
        f"as_of: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(pack['as_of']))}",
        "",
    ]
    for k, v in pack.items():
        if k in ("symbol", "as_of", "bars_5m"):
            continue
        lines.append(f"{k}: {v if v is not None else 'missing'}")
    lines.append("")
    lines.append("bars_5m (oldest→newest; t=unix s, buy_ratio = taker buy share):")
    for b in pack.get("bars_5m", []):
        lines.append(
            f"  {b['t']} o={b['o']} h={b['h']} l={b['l']} c={b['c']} v={b['v']} buy_ratio={b['buy_ratio'] if b['buy_ratio'] is not None else 'missing'}"
        )
    return "\n".join(lines)
