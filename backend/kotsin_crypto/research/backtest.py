"""Event-driven backtester that reuses the live code path: 1m bars → BarStore resampler → the same
strategy → the same sizing → the same ExitEngine. Only execution is modelled, because there is no
historical L2 book:

* entries fill at the OPEN of the 1m bar after the decision bar closes, plus slippage;
* stops fill at the stop (or at the open when a bar gaps through it) minus slippage, checked against
  the bar's low/high BEFORE the ratchet sees the bar's high (conservative);
* taker fee both ways; funding at every 8 h realization from the FUNDING: series when available.

Deterministic: the same bars and config always produce the same trades.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from ..bars.unified import TF_SECONDS, BarStore, UnifiedBar
from ..domain import ExitReason, Position, PosSide, Trade, new_id, to_json
from ..risk.exits import ExitEngine
from ..risk.limits import RiskLimits
from ..risk.sizing import size_position
from ..risk.wallet import Wallet
from ..strategy.base import Side, Signal, Strategy
from ..strategy.can2 import Can2, Can2Config
from ..strategy.keys import StrategyKey
from .env import ACTIONS, compute_obs, stop_after_action
from .rl.policies import policy_by_name

FUNDING_INTERVAL_S = 8 * 3600


@dataclass(frozen=True, slots=True)
class ProductSpec:
    contract_value: float
    maintenance_margin_pct: float


@dataclass(frozen=True)
class BacktestConfig:
    symbols: tuple[str, ...]
    start: int
    end: int
    tf: str = "5m"
    strategy: str = "CAN2"
    params: Mapping[str, Any] = field(default_factory=dict)
    initial_usd: float = 10_000.0
    taker_fee_rate: float = 0.0005
    slippage_bps: Mapping[str, float] = field(
        default_factory=lambda: {"BTCUSD": 0.5, "ETHUSD": 1.0, "SOLUSD": 2.0}
    )
    default_slippage_bps: float = 2.0
    apply_funding: bool = True
    limits: Mapping[str, Any] = field(default_factory=dict)
    # None → the hand R-ladder in risk/exits.py (default, byte-identical to before). Otherwise a
    # policy name ("ladder", "hold_only") or an artefact JSON path: exits then follow
    # research.env semantics (policy acts at each closed strategy-timeframe bar).
    exit_policy: str | None = None

    def slip(self, symbol: str) -> float:
        return float(self.slippage_bps.get(symbol, self.default_slippage_bps))


def make_strategy(name: str, params: Mapping[str, Any]) -> Strategy:
    if name.upper() == StrategyKey.CAN2.value:
        return Can2(Can2Config(**params))
    raise ValueError(f"unknown strategy {name}")


class _Ctx:
    def __init__(self, store: BarStore) -> None:
        self._store = store
        self.state: dict[str, object] = {}

    def bars(self, symbol: str, tf: str, n: int) -> list[UnifiedBar]:
        return self._store.bars(symbol, tf, n)


@dataclass(slots=True)
class _Pending:
    signal: Signal
    contracts: int
    leverage: float
    decided_ts: int


class BacktestRunner:
    def __init__(
        self,
        cfg: BacktestConfig,
        products: Mapping[str, ProductSpec],
        funding: Mapping[str, list[tuple[int, float]]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.products = products
        self.funding = {s: list(v) for s, v in (funding or {}).items()}
        self._funding_idx: dict[str, int] = dict.fromkeys(self.funding, 0)
        self.limits = RiskLimits(**dict(cfg.limits))
        self.strategy = make_strategy(cfg.strategy, cfg.params)
        self.store = BarStore(cfg.symbols)
        self.ctx = _Ctx(self.store)
        self.exits = ExitEngine(self.limits)
        self.exit_policy = policy_by_name(cfg.exit_policy) if cfg.exit_policy else None
        self.wallet = Wallet.new(self.strategy.key.value, cfg.initial_usd, now=cfg.start)
        self.positions: dict[str, Position] = {}
        self.pending: list[_Pending] = []
        self.trades: list[Trade] = []
        self.signals: list[dict[str, Any]] = []
        self.equity: list[tuple[int, float]] = []
        self.costs = {"fees": 0.0, "funding": 0.0, "slippage": 0.0}
        self.bars_1m = 0
        self.last_close: dict[str, float] = {}
        self._last_equity_ts = 0

    # ---- helpers ---------------------------------------------------------------------------------
    def _fee(self, price: float, contracts: int, cv: float) -> float:
        fee = price * contracts * cv * self.cfg.taker_fee_rate
        self.costs["fees"] += fee
        return fee

    def _slipped(
        self, symbol: str, ref: float, adverse_up: bool, contracts: int, cv: float
    ) -> float:
        amt = ref * self.cfg.slip(symbol) / 1e4
        self.costs["slippage"] += amt * contracts * cv
        return ref + amt if adverse_up else ref - amt

    def _funding_rate_at(self, symbol: str, ts: int) -> float | None:
        series = self.funding.get(symbol)
        if not series:
            return None
        i = self._funding_idx[symbol]
        while i + 1 < len(series) and series[i + 1][0] <= ts:
            i += 1
        self._funding_idx[symbol] = i
        return series[i][1] if series[i][0] <= ts else None

    # ---- main loop -------------------------------------------------------------------------------
    def run(self, bars_by_symbol: Mapping[str, Iterable[UnifiedBar]]) -> dict[str, Any]:
        """``bars_by_symbol`` must be ascending per symbol; symbols are merged in time order."""
        iters = {s: iter(b) for s, b in bars_by_symbol.items()}
        heads: dict[str, UnifiedBar | None] = {s: next(it, None) for s, it in iters.items()}
        while True:
            live = {s: b for s, b in heads.items() if b is not None}
            if not live:
                break
            sym = min(live, key=lambda s: (live[s].ts, s))
            self.on_1m(live[sym])
            heads[sym] = next(iters[sym], None)
        return self.finish()

    def on_1m(self, bar: UnifiedBar) -> None:
        self.bars_1m += 1
        sym = bar.symbol
        spec = self.products[sym]
        # 1. fills decided at an earlier close
        for p in [p for p in self.pending if p.signal.symbol == sym and p.decided_ts < bar.ts]:
            self.pending.remove(p)
            self._open(p, bar, spec)
        # 2. manage open positions on this symbol
        for pos in [p for p in self.positions.values() if p.symbol == sym]:
            self._manage(pos, bar, spec)
        # 3. funding at realization boundaries (bar start on an 8h boundary)
        if self.cfg.apply_funding and bar.ts % FUNDING_INTERVAL_S == 0:
            for pos in [p for p in self.positions.values() if p.symbol == sym]:
                rate = self._funding_rate_at(sym, bar.ts)
                if rate is not None:
                    amount = (
                        pos.contracts * pos.contract_value * bar.open * rate / 100 * pos.direction
                    )
                    pos.funding += amount
                    self.wallet.apply_funding(amount, bar.ts)
                    self.costs["funding"] += amount
        # 4. bars → strategy
        self.last_close[sym] = bar.close
        for hb in self.store.add_1m(bar):
            if hb.tf == self.cfg.tf and self.exit_policy is not None:
                for pos in [p for p in self.positions.values() if p.symbol == sym]:
                    self._policy_step(pos, hb, spec)
            if hb.tf == self.cfg.tf and hb.tf in self.strategy.timeframes:
                for sig in self.strategy.on_bar(self.ctx, hb):
                    self._on_signal(sig, hb, spec)
                if hb.end_ts - self._last_equity_ts >= 3600:
                    self._last_equity_ts = hb.end_ts
                    self.equity.append((hb.end_ts, self._equity()))
        self.wallet.rollover(bar.end_ts)

    def _equity(self) -> float:
        return self.wallet.balance + sum(
            p.unrealized(self.last_close.get(p.symbol, p.entry)) for p in self.positions.values()
        )

    def _on_signal(self, sig: Signal, bar: UnifiedBar, spec: ProductSpec) -> None:
        rec = {
            "signal_id": sig.signal_id,
            "symbol": sig.symbol,
            "side": sig.side.value,
            "ts": sig.ts,
            "entry": float(sig.entry),
            "stop": float(sig.stop),
            "reason": sig.reason,
            "evidence": dict(sig.evidence),
        }
        if self.wallet.halted:
            rec.update(decision="REJECTED_RISK", why=f"wallet halted: {self.wallet.halt_reason}")
        elif any(p.symbol == sig.symbol for p in self.positions.values()) or any(
            p.signal.symbol == sig.symbol for p in self.pending
        ):
            rec.update(decision="REJECTED_RISK", why="already in a position on this symbol")
        elif len(self.positions) + len(self.pending) >= self.limits.max_positions_total:
            rec.update(decision="REJECTED_RISK", why="max positions open")
        else:
            open_notional = sum(
                p.contracts * p.contract_value * self.last_close.get(p.symbol, p.entry)
                for p in self.positions.values()
            )
            sz = size_position(
                balance=self.wallet.balance,
                entry=float(sig.entry),
                stop=float(sig.stop),
                contract_value=spec.contract_value,
                maintenance_margin_pct=spec.maintenance_margin_pct,
                atr=float(sig.evidence.get("atr") or 0) or None,
                limits=self.limits,
                open_notional=open_notional,
            )
            if not sz.ok:
                rec.update(decision="REJECTED_RISK", why=f"sizing: {sz.reason}")
            else:
                rec.update(decision="PENDING_FILL", why="", contracts=sz.contracts)
                self.pending.append(_Pending(sig, sz.contracts, sz.leverage, bar.end_ts - 60))
        self.signals.append(rec)

    def _open(self, p: _Pending, bar: UnifiedBar, spec: ProductSpec) -> None:
        sig = p.signal
        side = PosSide.LONG if sig.side is Side.LONG else PosSide.SHORT
        fill = self._slipped(
            sig.symbol,
            bar.open,
            adverse_up=side is PosSide.LONG,
            contracts=p.contracts,
            cv=spec.contract_value,
        )
        dist = abs(float(sig.entry) - float(sig.stop))
        stop = fill - dist if side is PosSide.LONG else fill + dist
        fee = self._fee(fill, p.contracts, spec.contract_value)
        pos = Position(
            id=new_id("bt"),
            strategy=self.strategy.key.value,
            symbol=sig.symbol,
            side=side,
            contracts=p.contracts,
            entry=fill,
            stop=stop,
            initial_stop=stop,
            opened_ts=bar.ts,
            signal_id=sig.signal_id,
            contract_value=spec.contract_value,
            r_unit=dist,
            notional=fill * p.contracts * spec.contract_value,
            leverage=p.leverage,
            fees=fee,
        )
        self.wallet.apply_fee(fee, bar.ts)
        self.positions[pos.id] = pos

    def _manage(self, pos: Position, bar: UnifiedBar, spec: ProductSpec) -> None:
        long = pos.side is PosSide.LONG
        # stop first, against the bar's extreme, with the stop as it stood at the bar's open
        if (long and bar.open <= pos.stop) or (not long and bar.open >= pos.stop):
            self._close(
                pos,
                self._slipped(pos.symbol, bar.open, not long, pos.contracts, spec.contract_value),
                ExitReason.STOP,
                bar.ts,
                "gap through stop at open",
            )
            return
        if (long and bar.low <= pos.stop) or (not long and bar.high >= pos.stop):
            self._close(
                pos,
                self._slipped(pos.symbol, pos.stop, not long, pos.contracts, spec.contract_value),
                ExitReason.STOP,
                bar.ts,
                "stop hit intrabar",
            )
            return
        favourable = bar.high if long else bar.low
        if self.exit_policy is not None:
            # a learned/explicit policy owns the stop; only track excursions here
            pos.peak_r = max(pos.peak_r, pos.r_now(favourable))
            pos.mfe_r = max(pos.mfe_r, pos.peak_r)
            d = None
        else:
            # let the ratchet see the favourable extreme, and re-check against the close
            self.exits.on_mark(pos, favourable, bar.end_ts)
            d = self.exits.on_mark(pos, bar.close, bar.end_ts)
        if d is not None:
            self._close(
                pos,
                self._slipped(pos.symbol, bar.close, not long, pos.contracts, spec.contract_value),
                ExitReason.STOP,
                bar.end_ts,
                "ratcheted stop above close",
            )
            return
        adverse = bar.low if long else bar.high
        pos.mae_r = min(pos.mae_r, pos.r_now(adverse))
        t = self.exits.on_clock(pos, bar.close, bar.end_ts)
        if t is not None:
            self._close(
                pos,
                self._slipped(pos.symbol, bar.close, not long, pos.contracts, spec.contract_value),
                ExitReason.TIME_STOP,
                bar.end_ts,
                t.note,
            )

    def _policy_step(self, pos: Position, hb: UnifiedBar, spec: ProductSpec) -> None:
        """One decision of the configured exit policy at a closed strategy-timeframe bar."""
        assert self.exit_policy is not None
        window = self.store.bars(pos.symbol, hb.tf, 49)
        obs = compute_obs(
            side=pos.direction,
            entry=pos.entry,
            r_unit=pos.r_unit,
            peak_r=pos.peak_r,
            bars_held=pos.bars_held,
            window=window,
        )
        action = ACTIONS[self.exit_policy.act(obs)]
        pos.bars_held += 1
        long = pos.side is PosSide.LONG
        if action == "exit_now":
            self._close(
                pos,
                self._slipped(pos.symbol, hb.close, not long, pos.contracts, spec.contract_value),
                ExitReason.POLICY,
                hb.end_ts,
                "policy exit",
            )
            return
        pos.stop = stop_after_action(
            action,
            side=pos.direction,
            entry=pos.entry,
            r_unit=pos.r_unit,
            stop=pos.stop,
            peak_r=pos.peak_r,
        )

    def _close(self, pos: Position, price: float, reason: ExitReason, ts: int, note: str) -> None:
        fee = self._fee(price, pos.contracts, pos.contract_value)
        pos.fees += fee
        pnl = pos.unrealized(price)
        self.wallet.apply_fee(fee, ts)
        self.wallet.apply_close(pnl, ts)
        net = pnl - pos.fees - pos.funding
        pos.status, pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl = (
            "CLOSED",
            float(ts),
            price,
            reason.value,
            pnl,
        )
        self.trades.append(
            Trade(
                id=new_id("bt-trd"),
                position_id=pos.id,
                strategy=pos.strategy,
                symbol=pos.symbol,
                side=pos.side,
                contracts=pos.contracts,
                entry=pos.entry,
                exit=price,
                pnl=pnl,
                fees=pos.fees,
                funding=pos.funding,
                net=net,
                r_multiple=(price - pos.entry) * pos.direction / pos.r_unit if pos.r_unit else 0.0,
                mfe_r=pos.mfe_r,
                mae_r=pos.mae_r,
                exit_reason=reason.value,
                opened_ts=pos.opened_ts,
                closed_ts=float(ts),
                duration_s=float(ts - pos.opened_ts),
                signal_id=pos.signal_id,
                r_unit=pos.r_unit,
                contract_value=pos.contract_value,
            )
        )
        del self.positions[pos.id]
        self.wallet.check_breakers(self.limits, ts)

    def finish(self) -> dict[str, Any]:
        for pos in list(self.positions.values()):
            px = self.last_close.get(pos.symbol, pos.entry)
            self._close(pos, px, ExitReason.END, self.cfg.end, "range ended")
        self.equity.append((self.cfg.end, self.wallet.balance))
        return {
            "stats": self.stats(),
            "trades": [to_json(t) for t in self.trades],
            "equity": self.equity,
            "signals": self.signals[-1000:],
            "costs": self.costs,
        }

    def stats(self) -> dict[str, Any]:
        t = self.trades
        nets = [x.net for x in t]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n < 0]
        peak, mdd = self.cfg.initial_usd, 0.0
        for _, eq in self.equity:
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak * 100 if peak else 0.0)
        by_symbol: dict[str, dict[str, float]] = {}
        for x in t:
            d = by_symbol.setdefault(x.symbol, {"trades": 0, "net": 0.0, "wins": 0})
            d["trades"] += 1
            d["net"] += x.net
            d["wins"] += x.net > 0
        reasons: dict[str, int] = {}
        for x in t:
            reasons[x.exit_reason] = reasons.get(x.exit_reason, 0) + 1
        rejected: dict[str, int] = {}
        for s in self.signals:
            if s["decision"] != "PENDING_FILL":
                rejected[s["why"]] = rejected.get(s["why"], 0) + 1
        return {
            "trades": len(t),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(t) if t else None,
            "net": sum(nets),
            "gross": sum(x.pnl for x in t),
            "fees": self.costs["fees"],
            "funding": self.costs["funding"],
            "slippage": self.costs["slippage"],
            "avg_net": sum(nets) / len(t) if t else None,
            "avg_r": sum(x.r_multiple for x in t) / len(t) if t else None,
            "profit_factor": (sum(wins) / -sum(losses))
            if losses and wins
            else (math.inf if wins and not losses else None),
            "max_drawdown_pct": mdd,
            "final_balance": self.wallet.balance,
            "return_pct": (self.wallet.balance - self.cfg.initial_usd) / self.cfg.initial_usd * 100,
            "avg_hold_s": sum(x.duration_s for x in t) / len(t) if t else None,
            "exit_reasons": reasons,
            "signals": len(self.signals),
            "rejected": rejected,
            "by_symbol": by_symbol,
            "bars_1m": self.bars_1m,
            "days": (self.cfg.end - self.cfg.start) / 86_400,
            "halted": self.wallet.halted,
            "halt_reason": self.wallet.halt_reason,
        }


def config_from_dict(d: Mapping[str, Any]) -> BacktestConfig:
    return BacktestConfig(
        symbols=tuple(d["symbols"]),
        start=int(d["start"]),
        end=int(d["end"]),
        tf=str(d.get("tf", "5m")),
        strategy=str(d.get("strategy", "CAN2")),
        params=dict(d.get("params") or {}),
        initial_usd=float(d.get("initial_usd", 10_000.0)),
        taker_fee_rate=float(d.get("taker_fee_rate", 0.0005)),
        slippage_bps=dict(d.get("slippage_bps") or {"BTCUSD": 0.5, "ETHUSD": 1.0, "SOLUSD": 2.0}),
        default_slippage_bps=float(d.get("default_slippage_bps", 2.0)),
        apply_funding=bool(d.get("apply_funding", True)),
        exit_policy=d.get("exit_policy") or None,
        limits=dict(d.get("limits") or {}),
    )


def config_to_dict(cfg: BacktestConfig) -> dict[str, Any]:
    d = asdict(cfg)
    d["symbols"] = list(cfg.symbols)
    return d


__all__ = [
    "TF_SECONDS",
    "BacktestConfig",
    "BacktestRunner",
    "ProductSpec",
    "config_from_dict",
    "config_to_dict",
]
