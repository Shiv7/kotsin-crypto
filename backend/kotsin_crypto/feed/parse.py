"""Normalise raw public-feed messages into small typed events. Shapes verified live on 2026-09-20:

trades          {"p","s","r","sy","t","ts"}              r == "t" ⇔ the buyer was the taker
ob_l1           {"bp","bs","ap","as","sy","ts","lts"}
ob_l2           {"b":[[px,sz]…],"a":[[px,sz]…],"sy","ts"}   15 levels each side
mark_price      {"p","sy":"MARK:<sym>","ts"}
funding_rate    {"fr","fi","nfr","sy","ts"}                fr is a PERCENT per interval (0.01 = 0.01%)
candlestick_1m  {"o","h","l","c","v","cst","res","sy","ts"} forming candle; final when cst advances
ticker          {"d":[{"m","oi":[contracts,…],"q":[bid,bs,ask,as,…],"pb":[lo,hi],"to":[usd,…]}],"sp","sy","ts"}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .book import Level


@dataclass(slots=True)
class TradeEvt:
    symbol: str
    ts_us: int  # match-engine time
    price: float
    size: float
    taker_buy: bool
    pub_ts_us: int = 0  # venue publish time (≈ 260 ms after the match)


@dataclass(slots=True)
class L1Evt:
    symbol: str
    ts_us: int
    bid: float
    bid_size: int
    ask: float
    ask_size: int


@dataclass(slots=True)
class L2Evt:
    symbol: str
    ts_us: int
    bids: list[Level]
    asks: list[Level]


@dataclass(slots=True)
class MarkEvt:
    symbol: str
    ts_us: int
    price: float


@dataclass(slots=True)
class FundingEvt:
    symbol: str
    ts_us: int
    rate_pct: float
    interval_s: int
    next_ts_us: int


@dataclass(slots=True)
class CandleEvt:
    symbol: str
    start_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts_us: int


@dataclass(slots=True)
class TickerEvt:
    symbol: str
    ts_us: int
    mark: float | None
    spot: float | None
    oi_contracts: float | None
    bid: float | None
    ask: float | None
    band_lo: float | None
    band_hi: float | None
    turnover_usd: float | None


@dataclass(slots=True)
class StatusEvt:
    ts_us: int
    status: str
    event: str


Event = TradeEvt | L1Evt | L2Evt | MarkEvt | FundingEvt | CandleEvt | TickerEvt | StatusEvt


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse(msg: dict[str, Any]) -> Event | None:
    t = msg.get("type")
    try:
        if t == "trades":
            return TradeEvt(
                msg["sy"], int(msg["t"]), float(msg["p"]), float(msg["s"]), msg.get("r") == "t"
            )
        if t == "ob_l1":
            return L1Evt(
                msg["sy"],
                int(msg["ts"]),
                float(msg["bp"]),
                int(float(msg["bs"])),
                float(msg["ap"]),
                int(float(msg["as"])),
            )
        if t == "ob_l2":
            return L2Evt(
                msg["sy"],
                int(msg["ts"]),
                [Level(float(p), int(float(s))) for p, s in msg.get("b", [])],
                [Level(float(p), int(float(s))) for p, s in msg.get("a", [])],
            )
        if t == "mark_price":
            sym = str(msg["sy"]).removeprefix("MARK:")
            return MarkEvt(sym, int(msg["ts"]), float(msg["p"]))
        if t == "funding_rate":
            return FundingEvt(
                msg["sy"], int(msg["ts"]), float(msg["fr"]), int(msg["fi"]), int(msg["nfr"])
            )
        if t == "candlestick_1m":
            return CandleEvt(
                msg["sy"],
                int(msg["cst"]) // 1_000_000,
                float(msg["o"]),
                float(msg["h"]),
                float(msg["l"]),
                float(msg["c"]),
                float(msg.get("v") or 0.0),
                int(msg["ts"]),
            )
        if t == "ticker":
            d = (msg.get("d") or [{}])[0]
            q = d.get("q") or []
            pb = d.get("pb") or []
            oi = d.get("oi") or []
            to = d.get("to") or []
            return TickerEvt(
                symbol=msg.get("sy") or d.get("s"),
                ts_us=int(msg["ts"]),
                mark=_f(d.get("m")),
                spot=_f(msg.get("sp")),
                oi_contracts=_f(oi[0]) if oi else None,
                bid=_f(q[0]) if len(q) > 0 else None,
                ask=_f(q[2]) if len(q) > 2 else None,
                band_lo=_f(pb[0]) if len(pb) > 0 else None,
                band_hi=_f(pb[1]) if len(pb) > 1 else None,
                turnover_usd=_f(to[0]) if to else None,
            )
        if t == "system_status":
            return StatusEvt(
                int(msg.get("timestamp") or 0), str(msg.get("status")), str(msg.get("event"))
            )
    except (KeyError, TypeError, ValueError):
        return None
    return None
