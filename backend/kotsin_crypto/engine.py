"""The engine: one asyncio process wiring feed → archive/books/bars → strategies → risk → gateway →
ledger. Everything here is synchronous per event; persistence is queued to a single writer task so a
slow disk never delays a decision. The exchange feed is the clock; a 1 s ticker closes quiet minutes,
applies time-stops and funding, rolls wallets over at UTC midnight and writes hourly health rows.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Coroutine
from dataclasses import asdict
from typing import Any

import structlog

from .bars.backfill import fetch_1m
from .bars.micro import MicroBuilder
from .bars.trade_bar import TradeBar, TradeBarBuilder
from .bars.unified import BarStore, UnifiedBar, merge_1m
from .bus import Bus
from .committee.service import CommitteeService
from .config import Settings
from .domain import (
    ExitDecision,
    ExitReason,
    OrderIntent,
    OrderSide,
    Position,
    PosSide,
    Purpose,
    Trade,
    new_id,
    to_json,
)
from .exec.gateway import (
    LIVE_MODES,
    Decision,
    Gateway,
    LiveCaps,
    LiveContext,
    Mode,
    OrderResult,
    live_capped_contracts,
)
from .exec.live import LiveExecutor
from .exec.paper import PaperMatcher
from .exec.reconcile import Reconciler
from .feed.archive import Archive
from .feed.book import Book
from .feed.parse import (
    CandleEvt,
    FundingEvt,
    L1Evt,
    L2Evt,
    MarkEvt,
    StatusEvt,
    TickerEvt,
    TradeEvt,
    parse,
)
from .ledger.db import Ledger
from .ops.telegram import Telegram
from .research.jobs import BacktestJobs
from .risk.exits import ExitEngine
from .risk.limits import RiskLimits
from .risk.sizing import size_position
from .risk.wallet import Wallet
from .strategy.base import Side, Signal, Strategy
from .strategy.can2 import Can2
from .venue.delta.catalogue import Catalogue
from .venue.delta.market import MarketData
from .venue.delta.rest import DeltaRest
from .venue.delta.ws_private import (
    DeltaPrivateWS,
    OrderEvt,
    PositionEvt,
    PrivateEvent,
    UserTradeEvt,
)
from .venue.delta.ws_public import DeltaPublicWS, channels_for

log = structlog.get_logger("engine")


class StrategyContext:
    def __init__(self, store: BarStore) -> None:
        self._store = store
        self.state: dict[str, object] = {}

    def bars(self, symbol: str, tf: str, n: int) -> list[UnifiedBar]:
        return self._store.bars(symbol, tf, n)


class Engine:
    def __init__(self, settings: Settings, bus: Bus, telegram: Telegram) -> None:
        self.settings = settings
        self.bus = bus
        self.telegram = telegram
        self.symbols = settings.symbol_list
        self.started_ts = time.time()
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []

        self.rest = DeltaRest(settings)
        self.catalogue: Catalogue | None = None
        self.market = MarketData(self.rest)
        self.backtests: BacktestJobs | None = None
        self.committee = CommitteeService(self, settings)
        self.ledger = Ledger(settings.db_url)
        self.control: dict[str, Any] = {"mode": "SHADOW", "halted": False, "halt_reason": ""}

        self.archive = Archive(settings.data_dir / "archive")
        self.books: dict[str, Book] = {s: Book(s) for s in self.symbols}
        self.trade_builders: dict[str, TradeBarBuilder] = {
            s: TradeBarBuilder(s) for s in self.symbols
        }
        self.micro_builders: dict[str, MicroBuilder] = {s: MicroBuilder(s) for s in self.symbols}
        self.store = BarStore(self.symbols)
        self.marks: dict[str, float] = {}
        self.last_price: dict[str, float] = {}
        self.spot: dict[str, float] = {}
        self.oi: dict[str, float] = {}
        self.funding: dict[str, FundingEvt] = {}
        self.funding_applied: dict[str, int] = {}  # position id → last applied next_ts_us
        self.system_status = "unknown"
        self._delta_candles: dict[str, CandleEvt] = {}
        self._final_delta_candles: dict[str, dict[int, CandleEvt]] = {}
        self.candle_check = {
            "compared": 0,
            "ohlc_match": 0,
            "volume_match": 0,
            "examples": deque(maxlen=12),
        }
        # WS candlestick_1m buckets trades by publish time, so boundary trades shift a minute: that
        # comparison is a "boundary shift" counter. The authoritative check is against REST candles.
        self.rest_check: dict[str, Any] = {
            "compared": 0,
            "ohlc_match": 0,
            "volume_match": 0,
            "last_run_ts": None,
            "examples": deque(maxlen=12),
        }
        self._rest_checked: set[tuple[str, int]] = set()
        self._last_rest_check = 0.0

        self.limits = RiskLimits()
        self.exits = ExitEngine(self.limits)
        self.strategies: list[Strategy] = [Can2()]
        self.contexts: dict[str, StrategyContext] = {
            s.key.value: StrategyContext(self.store) for s in self.strategies
        }
        self.wallets: dict[str, Wallet] = {}
        self.positions: dict[str, Position] = {}
        self.matcher = PaperMatcher()
        self.live_caps = LiveCaps(
            symbols=tuple(settings.live_symbol_list),
            max_contracts=settings.live_max_contracts,
            max_positions=settings.live_max_positions,
            max_orders_per_day=settings.live_max_orders_per_day,
            daily_loss_usd=settings.live_daily_loss_usd,
            leverage=settings.live_leverage,
        )
        self.gateway = Gateway(
            books=self.books,
            matcher=self.matcher,
            mode=self._mode,
            halted=self._halted,
            caps=self.live_caps,
        )
        # Live execution (only when API keys exist): executor, private feed, reconciler.
        self.live: LiveExecutor | None = None
        self.live_ws: DeltaPrivateWS | None = None
        self.reconciler = Reconciler(self)
        self.venue_positions: dict[str, dict[str, Any]] = {}
        self.user_trades: deque[dict[str, Any]] = deque(maxlen=200)
        self._pending_entries: set[str] = set()
        self._live_tasks: set[asyncio.Task[None]] = set()

        self.ws = DeltaPublicWS(
            settings.endpoints.ws_public,
            channels_for(self.symbols),
            self.on_ws_message,
            force_ipv4=settings.delta_force_ipv4,
        )
        self._db_queue: asyncio.Queue[Coroutine[Any, Any, Any]] = asyncio.Queue(maxsize=10_000)
        self.counters: dict[str, int] = defaultdict(int)
        self.recent_signals: deque[dict[str, Any]] = deque(maxlen=200)
        self.last_bar_ts: dict[str, int] = {}
        self._last_hour_key: str | None = None

    # ---- lifecycle -----------------------------------------------------------------------------
    async def start(self) -> None:
        s = self.settings
        s.data_dir.mkdir(parents=True, exist_ok=True)
        await self.ledger.init()
        self.control = await self.ledger.get_control()
        self.catalogue = await Catalogue.load(self.rest)
        missing = [x for x in self.symbols if x not in self.catalogue]
        if missing:
            raise RuntimeError(f"symbols not in the live catalogue: {missing}")
        self.backtests = BacktestJobs(
            self.rest, s.data_dir / "history", s.data_dir / "backtests", self.catalogue
        )
        btc = self.catalogue.by_symbol(self.symbols[0])
        taker = float(btc.taker_fee or 0.0005)
        self.matcher = PaperMatcher(taker_fee_rate=taker)
        if s.has_api_keys:
            assert s.delta_api_key and s.delta_api_secret
            self.live = LiveExecutor(
                self.rest,
                self.catalogue,
                leverage=s.live_leverage,
                taker_fee_rate=taker,
                audit=self._audit_live,
                notify=self.telegram.send,
            )
            self.live_ws = DeltaPrivateWS(
                s.endpoints.ws_private,
                s.delta_api_key.get_secret_value(),
                s.delta_api_secret.get_secret_value(),
                self.symbols,
                self.on_private_event,
                force_ipv4=s.delta_force_ipv4,
            )
        self.gateway = Gateway(
            books=self.books,
            matcher=self.matcher,
            mode=self._mode,
            halted=self._halted,
            caps=self.live_caps,
            live=self.live,
        )
        for coid in await self.ledger.known_client_order_ids():
            self.gateway.remember(coid)
        await self.check_arm_at_boot()

        saved = await self.ledger.load_wallets()
        for strat in self.strategies:
            key = strat.key.value
            self.wallets[key] = (
                Wallet.from_json(saved[key])
                if key in saved
                else Wallet.new(key, s.paper_initial_usd)
            )
            await self.ledger.upsert_wallet(key, self.wallets[key].to_json())
        for raw in await self.ledger.load_open_positions():
            pos = Position(**{k: v for k, v in raw.items() if k in Position.__dataclass_fields__})
            pos.side = PosSide(pos.side)
            self.positions[pos.id] = pos

        for sym in self.symbols:
            try:
                t = await self.rest.ticker(sym)
                # ticker "size" is 24h volume in CONTRACTS; "volume" is in the underlying unit (BTC)
                daily_volume = float(t.get("size") or 0) or None
                if daily_volume is None and t.get("volume"):
                    cv = float(self.catalogue.by_symbol(sym).contract_value)
                    daily_volume = float(t["volume"]) / cv if cv else None
            except Exception as exc:
                log.warning("daily_volume_unavailable", symbol=sym, error=str(exc))
                daily_volume = None
            self.micro_builders[sym] = MicroBuilder(sym, daily_volume=daily_volume)
            bars = await fetch_1m(self.rest, sym, hours=s.backfill_hours)
            for b in bars:
                self.store.add_1m(b)
            if bars:
                last = bars[-1]
                self.trade_builders[sym] = TradeBarBuilder(
                    sym, last_close=last.close, last_minute=last.ts // 60
                )
                self.last_bar_ts[sym] = last.ts

        self.tasks = [
            asyncio.create_task(self.ws.run(self.stop_event), name="ws"),
            asyncio.create_task(self.archive.run(self.stop_event), name="archive"),
            asyncio.create_task(self._clock(), name="clock"),
            asyncio.create_task(self._db_writer(), name="db"),
            asyncio.create_task(self.committee.run(self.stop_event), name="committee"),
        ]
        if self.live_ws is not None:
            self.tasks.append(
                asyncio.create_task(self.live_ws.run(self.stop_event), name="ws_priv")
            )
            self.tasks.append(
                asyncio.create_task(self.reconciler.run(self.stop_event), name="reconcile")
            )
            if self.is_live_mode():
                try:
                    await self.reconciler.reconcile_once()
                except Exception:
                    log.exception("boot_reconcile_failed")
        await self.ledger.event(
            "boot",
            {
                "mode": self.control["mode"],
                "symbols": self.symbols,
                "positions": len(self.positions),
            },
        )
        log.info(
            "engine_started",
            mode=self.control["mode"],
            symbols=self.symbols,
            wallets={k: w.balance for k, w in self.wallets.items()},
            open_positions=len(self.positions),
        )

    async def stop(self) -> None:
        self.stop_event.set()
        if self._live_tasks:  # let an in-flight venue order finish rather than orphan it
            await asyncio.wait(list(self._live_tasks), timeout=LiveExecutor.FILL_TIMEOUT_S + 5)
        for t in [*self.tasks, *self._live_tasks]:
            t.cancel()
        await asyncio.gather(*self.tasks, *self._live_tasks, return_exceptions=True)
        while not self._db_queue.empty():
            coro = self._db_queue.get_nowait()
            try:
                await coro
            except Exception:
                log.exception("db_flush_failed")
        for key, w in self.wallets.items():
            await self.ledger.upsert_wallet(key, w.to_json())
        for pos in self.positions.values():
            await self.ledger.upsert_position(to_json(pos))
        self.archive.flush()
        self.archive.close()
        await self.ledger.event("shutdown", {"open_positions": len(self.positions)})
        await self.ledger.close()
        await self.rest.aclose()

    # ---- control ---------------------------------------------------------------------------------
    def _mode(self) -> Mode:
        return Mode(self.control.get("mode", "SHADOW"))

    def is_live_mode(self) -> bool:
        return self._mode() in LIVE_MODES

    def live_armed(self, now: float | None = None) -> bool:
        """A live mode counts as armed only while ``armed_until`` is in the future."""
        until = self.control.get("armed_until")
        return bool(until) and float(until) > (now or time.time())

    def _halted(self) -> tuple[bool, str]:
        if self.control.get("halted"):
            return True, str(self.control.get("halt_reason") or "halted")
        if self.gateway.breaker_tripped:
            return True, "gateway breaker tripped"
        return False, ""

    async def set_mode(self, mode: str, arm_hours: float | None = None) -> dict[str, Any]:
        m = Mode(mode)
        armed_until: float | None = None
        if m in LIVE_MODES:
            if not self.settings.has_api_keys or self.live is None:
                raise ValueError("LIVE modes need KC_DELTA_API_KEY / KC_DELTA_API_SECRET")
            if arm_hours is None or not (0.5 <= arm_hours <= 24):
                raise ValueError("LIVE modes must be armed: pass arm_hours between 0.5 and 24")
            armed_until = time.time() + arm_hours * 3600
        await self.ledger.set_mode(m.value, armed_until)
        self.control = await self.ledger.get_control()
        await self.ledger.event("mode", {"mode": m.value, "armed_until": armed_until})
        await self.telegram.send(
            f"mode → {m.value}"
            + (
                f" (armed for {arm_hours:g}h, {self.settings.delta_env.value})"
                if armed_until
                else ""
            )
        )
        log.info("mode_changed", mode=m.value, armed_until=armed_until)
        if m in LIVE_MODES and self.live is not None:
            try:
                await self.reconciler.reconcile_once()
            except Exception:
                log.exception("arm_reconcile_failed")
        return self.control

    async def set_halt(
        self, halted: bool, reason: str = "", *, flatten: bool = True
    ) -> dict[str, Any]:
        await self.ledger.set_halt(halted, reason)
        self.control = await self.ledger.get_control()
        await self.ledger.event("halt", {"halted": halted, "reason": reason, "flatten": flatten})
        await self.telegram.send(f"{'HALT' if halted else 'resume'} {reason}".strip())
        if halted and flatten:
            self._flatten_all(ExitReason.HALT, reason or "manual halt")
        return self.control

    async def halt_entries(self, reason: str) -> dict[str, Any]:
        """Block new entries without touching open positions (reconcile, arm expiry)."""
        return await self.set_halt(True, reason, flatten=False)

    async def resume_entries(self, note: str = "") -> dict[str, Any]:
        return await self.set_halt(False, note, flatten=False)

    async def kill(self, reason: str) -> dict[str, Any]:
        """Halt entries, then cancel every venue order and close every venue position at market."""
        await self.set_halt(True, reason, flatten=not self.is_live_mode())
        out: dict[str, Any] = {"control": self.control, "live": None}
        if self.live is not None:
            res = await self.live.kill()
            out["live"] = {"ok": res.ok, "error": res.error}
            try:
                out["reconcile"] = (await self.reconciler.reconcile_once()).to_json()
            except Exception as exc:
                out["reconcile"] = {"error": str(exc)}
        return out

    async def check_arm_at_boot(self) -> bool:
        """R9: a live mode survives a restart only while armed (and only with keys); otherwise the
        engine boots into PAPER and says so. Returns True when it disarmed."""
        if not self.is_live_mode() or (self.live is not None and self.live_armed()):
            return False
        why = "no API keys" if self.live is None else "arm expired"
        await self.ledger.set_mode(Mode.PAPER.value, None)
        self.control = await self.ledger.get_control()
        await self.ledger.event("disarm", {"reason": why, "at": "boot"})
        await self.telegram.send(f"LIVE disarmed at boot ({why}) → PAPER")
        log.warning("live_disarmed_at_boot", reason=why)
        return True

    async def _disarm(self, why: str) -> None:
        """Arm expired: drop to PAPER, flatten NOTHING (venue-side bracket stops stay), alert."""
        await self.ledger.set_mode(Mode.PAPER.value, None)
        self.control = await self.ledger.get_control()
        await self.ledger.event("disarm", {"reason": why, "open_positions": len(self.positions)})
        await self.telegram.send(
            f"LIVE disarmed ({why}) → PAPER; {len(self.positions)} position(s) left with venue stops"
        )
        log.warning("live_disarmed", reason=why, open_positions=len(self.positions))

    def _audit_live(self, kind: str, data: dict[str, Any]) -> None:
        self._persist(self.ledger.event(kind, data))

    def atr_hint(self, symbol: str) -> float | None:
        """Rough ATR from recent 5m bars, for stops on adopted positions."""
        bars = self.store.bars(symbol, "5m", 15)
        if len(bars) < 2:
            return None
        trs = [
            max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            for i in range(1, len(bars))
        ]
        return sum(trs) / len(trs) if trs else None

    def live_context(self, symbol: str) -> LiveContext:
        w = next(iter(self.wallets.values()), None)
        return LiveContext(
            balance=w.balance if w else 0.0,
            open_positions=len([p for p in self.positions.values() if p.status == "OPEN"])
            + len(self._pending_entries),
            day_pnl_usd=w.day_pnl if w else 0.0,
            price=self.marks.get(symbol) or self.last_price.get(symbol) or 0.0,
        )

    # ---- private feed ----------------------------------------------------------------------------
    def on_private_event(self, evt: PrivateEvent, raw: dict[str, Any]) -> None:
        if isinstance(evt, OrderEvt):
            self.counters["private_orders"] += 1
            if self.live is not None:
                self.live.note_order_update(evt.client_order_id, raw)
            if evt.reason in ("stop_trigger", "liquidation"):
                log.warning("venue_order_event", reason=evt.reason, symbol=evt.symbol, raw=raw)
                self._persist(self.ledger.event("venue_order", raw))
                self._persist(
                    self.telegram.send(f"VENUE {evt.reason} on {evt.symbol}: order {evt.order_id}")
                )
        elif isinstance(evt, PositionEvt):
            self.counters["private_positions"] += 1
            self.venue_positions[evt.symbol] = {
                "size": evt.size,
                "entry_price": evt.entry_price,
                "margin": evt.margin,
                "action": evt.action,
                "ts": time.time(),
            }
        elif isinstance(evt, UserTradeEvt):
            self.counters["private_fills"] += 1
            self.user_trades.appendleft(to_json(evt))
            self._persist(self.ledger.event("venue_fill", to_json(evt)))
            if evt.reason in ("liquidation", "adl"):
                self._persist(
                    self.telegram.send(
                        f"VENUE {evt.reason.upper()} fill {evt.side} {evt.size} {evt.symbol} @ {evt.price}"
                    )
                )

    # ---- feed dispatch ---------------------------------------------------------------------------
    def on_ws_message(self, msg: dict[str, Any], raw: str, recv_us: int) -> None:
        t = msg.get("type", "?")
        self.archive.append(t, raw, recv_us)
        evt = parse(msg)
        if evt is None:
            self.counters["unparsed"] += 1
            return
        now = recv_us / 1e6
        if isinstance(evt, TradeEvt):
            self.last_price[evt.symbol] = evt.price
            self.micro_builders[evt.symbol].on_trade(
                evt.pub_ts_us or evt.ts_us, evt.price, evt.size, evt.taker_buy
            )
            bars = self.trade_builders[evt.symbol].on_trade(
                evt.ts_us, evt.price, evt.size, evt.taker_buy
            )
            if bars:
                self._on_1m_bars(evt.symbol, bars, now)
        elif isinstance(evt, L1Evt):
            self.micro_builders[evt.symbol].on_quote(
                evt.ts_us, evt.bid, evt.bid_size, evt.ask, evt.ask_size
            )
        elif isinstance(evt, L2Evt):
            self.books[evt.symbol].replace(evt.bids, evt.asks, evt.ts_us)
            self.micro_builders[evt.symbol].on_l2(evt.ts_us, evt.bids, evt.asks)
        elif isinstance(evt, MarkEvt):
            self.marks[evt.symbol] = evt.price
            self._check_stops(evt.symbol, evt.price, now)
        elif isinstance(evt, FundingEvt):
            self.funding[evt.symbol] = evt
        elif isinstance(evt, TickerEvt):
            if evt.oi_contracts is not None:
                self.oi[evt.symbol] = evt.oi_contracts
            if evt.spot is not None:
                self.spot[evt.symbol] = evt.spot
        elif isinstance(evt, CandleEvt):
            self._on_delta_candle(evt)
        elif isinstance(evt, StatusEvt):
            if evt.status != self.system_status:
                log.warning("system_status", status=evt.status, evt=evt.event)
                self.system_status = evt.status

    def _on_1m_bars(self, symbol: str, bars: list[TradeBar], now: float) -> None:
        connect_ts = self.ws.connect_ts or 0.0
        for tb in bars:
            book_bar = self.micro_builders[symbol].close(tb.ts)
            # A minute that started before the socket (re)connected is missing its first trades:
            # keep it (one minute inside a 5m bar) but tag it and keep it out of the determinism check.
            partial = tb.trade_count > 0 and tb.ts < connect_ts
            if partial:
                self.counters["bars_partial"] += 1
            u = merge_1m(
                tb,
                book_bar,
                oi=self.oi.get(symbol),
                funding_rate=(self.funding[symbol].rate_pct if symbol in self.funding else None),
                mark_close=self.marks.get(symbol),
                source="partial" if partial else "live",
            )
            closed = self.store.add_1m(u)
            self.last_bar_ts[symbol] = u.ts
            self.counters["bars_1m"] += 1
            self._compare_with_delta(u)
            for hb in closed:
                self.counters[f"bars_{hb.tf}"] += 1
                for strat in self.strategies:
                    if hb.tf in strat.timeframes:
                        try:
                            for sig in strat.on_bar(self.contexts[strat.key.value], hb):
                                self._on_signal(sig, hb, now)
                        except Exception:
                            self.counters["strategy_errors"] += 1
                            log.exception("strategy_failed", strategy=strat.key.value)

    # ---- candle cross-check ----------------------------------------------------------------------
    def _on_delta_candle(self, evt: CandleEvt) -> None:
        prev = self._delta_candles.get(evt.symbol)
        if prev is not None and evt.start_ts > prev.start_ts:
            self._final_delta_candles.setdefault(evt.symbol, {})[prev.start_ts] = prev
            ours = next(
                (
                    b
                    for b in reversed(self.store.bars(evt.symbol, "1m", 5))
                    if b.ts == prev.start_ts
                ),
                None,
            )
            if ours is not None and ours.source == "live":
                self._record_candle_check(ours, prev)
        self._delta_candles[evt.symbol] = evt

    def _compare_with_delta(self, ours: UnifiedBar) -> None:
        theirs = self._final_delta_candles.get(ours.symbol, {}).pop(ours.ts, None)
        if theirs is not None:
            self._record_candle_check(ours, theirs)

    def _record_candle_check(self, ours: UnifiedBar, theirs: CandleEvt) -> None:
        assert self.catalogue is not None
        tick = float(self.catalogue.by_symbol(ours.symbol).tick_size)
        c = self.candle_check
        c["compared"] += 1
        ohlc_ok = all(
            abs(a - b) <= tick + 1e-9
            for a, b in (
                (ours.open, theirs.open),
                (ours.high, theirs.high),
                (ours.low, theirs.low),
                (ours.close, theirs.close),
            )
        )
        vol_ok = abs(ours.volume - theirs.volume) <= max(1.0, 0.01 * theirs.volume)
        c["ohlc_match"] += ohlc_ok
        c["volume_match"] += vol_ok
        if not (ohlc_ok and vol_ok):
            c["examples"].append(
                {
                    "symbol": ours.symbol,
                    "ts": ours.ts,
                    "ours": [ours.open, ours.high, ours.low, ours.close, ours.volume],
                    "delta": [theirs.open, theirs.high, theirs.low, theirs.close, theirs.volume],
                }
            )

    async def verify_against_rest(self) -> None:
        """Compare our closed live 1m bars with Delta's REST candles (trade-time bucketed, like ours)."""
        assert self.catalogue is not None
        now = int(time.time())
        for sym in self.symbols:
            ours = [
                b
                for b in self.store.bars(sym, "1m", 30)
                if b.source == "live" and (sym, b.ts) not in self._rest_checked and b.ts + 120 < now
            ]
            if not ours:
                continue
            try:
                rows = {
                    int(c["time"]): c
                    for c in await self.rest.candles(sym, "1m", ours[0].ts, ours[-1].ts + 60)
                }
            except Exception as exc:
                log.warning("rest_check_failed", symbol=sym, error=str(exc))
                continue
            tick = float(self.catalogue.by_symbol(sym).tick_size)
            for b in ours:
                c = rows.get(b.ts)
                if c is None:
                    continue
                self._rest_checked.add((sym, b.ts))
                rc = self.rest_check
                rc["compared"] += 1
                theirs = [
                    float(c["open"]),
                    float(c["high"]),
                    float(c["low"]),
                    float(c["close"]),
                    float(c.get("volume") or 0.0),
                ]
                ohlc_ok = all(
                    abs(a - t) <= tick + 1e-9
                    for a, t in zip((b.open, b.high, b.low, b.close), theirs[:4], strict=True)
                )
                vol_ok = abs(b.volume - theirs[4]) <= max(1.0, 0.001 * theirs[4])
                rc["ohlc_match"] += ohlc_ok
                rc["volume_match"] += vol_ok
                if not (ohlc_ok and vol_ok):
                    rc["examples"].append(
                        {
                            "symbol": sym,
                            "ts": b.ts,
                            "ours": [b.open, b.high, b.low, b.close, b.volume],
                            "rest": theirs,
                        }
                    )
            self.rest_check["last_run_ts"] = time.time()
        if len(self._rest_checked) > 5000:
            self._rest_checked = set(sorted(self._rest_checked, key=lambda x: x[1])[-2000:])

    # ---- signals → risk → gateway ----------------------------------------------------------------
    def _on_signal(self, sig: Signal, bar: UnifiedBar, now: float) -> None:
        assert self.catalogue is not None
        self.counters["signals"] += 1
        key = sig.strategy.value
        wallet = self.wallets[key]
        product = self.catalogue.by_symbol(sig.symbol)
        entry, stop = float(sig.entry), float(sig.stop)
        record = {
            "signal_id": sig.signal_id,
            "strategy": key,
            "symbol": sig.symbol,
            "side": sig.side.value,
            "ts": sig.ts,
            "entry": entry,
            "stop": stop,
            "confidence": sig.confidence,
            "reason": sig.reason,
            "gates": [asdict(g) for g in sig.gates],
            "evidence": dict(sig.evidence),
        }

        def reject(decision: str, why: str) -> None:
            self.counters[f"reject_{decision}"] += 1
            record.update(decision=decision, decision_reason=why)
            self.recent_signals.appendleft(record)
            self._persist(
                self.ledger.insert_signal(
                    sig.signal_id, key, sig.symbol, sig.side.value, sig.ts, decision, why, record
                )
            )
            log.info(
                "signal_rejected",
                **{k: record[k] for k in ("signal_id", "symbol", "side")},
                decision=decision,
                why=why,
            )

        if wallet.halted:
            return reject(Decision.REJECTED_RISK.value, f"wallet halted: {wallet.halt_reason}")
        open_same = [p for p in self.positions.values() if p.symbol == sig.symbol]
        if len(open_same) >= self.limits.max_positions_per_symbol:
            return reject(Decision.REJECTED_RISK.value, "already in a position on this symbol")
        if len(self.positions) >= self.limits.max_positions_total:
            return reject(Decision.REJECTED_RISK.value, "max positions open")
        mark = self.marks.get(sig.symbol) or self.last_price.get(sig.symbol) or entry
        open_notional = sum(
            p.contracts * p.contract_value * self.marks.get(p.symbol, p.entry)
            for p in self.positions.values()
        )
        sizing = size_position(
            balance=wallet.balance,
            entry=entry,
            stop=stop,
            contract_value=float(product.contract_value),
            maintenance_margin_pct=float(product.maintenance_margin_pct or 0.5),
            atr=float(sig.evidence.get("atr") or 0) or None,
            limits=self.limits,
            open_notional=open_notional,
        )
        record["sizing"] = asdict(sizing)
        cv = float(product.contract_value)
        if self._mode() is Mode.LIVE_CAPPED:
            # Tiny-account rule: min(cap, max(1, risk-based)) if one contract fits inside
            # balance × live leverage. Only hard sizing failures still reject.
            if sizing.reason in ("invalid inputs", "liquidation too close"):
                return reject(Decision.REJECTED_RISK.value, f"sizing: {sizing.reason}")
            contracts, why = live_capped_contracts(
                sizing.contracts,
                self.live_caps,
                balance=wallet.balance,
                price=mark,
                contract_value=cv,
            )
            if contracts == 0:
                return reject(Decision.REJECTED_CAP.value, why)
            record["live"] = {"contracts": contracts, "sizing_reason": sizing.reason}
        elif not sizing.ok:
            return reject(Decision.REJECTED_RISK.value, f"sizing: {sizing.reason}")
        else:
            contracts = sizing.contracts
        cm, cwhy = self.committee.size_multiplier(sig.symbol, sig.side.value)
        record["committee"] = {
            "multiplier": cm,
            "why": cwhy,
            "applied": self.settings.committee_size_influence and cm < 1.0,
        }
        if self.settings.committee_size_influence and cm < 1.0:
            contracts = max(1, int(contracts * cm))
        intent = OrderIntent(
            strategy=key,
            symbol=sig.symbol,
            side=OrderSide.BUY if sig.side is Side.LONG else OrderSide.SELL,
            contracts=contracts,
            purpose=Purpose.ENTRY,
            signal_id=sig.signal_id,
            client_order_id=sig.signal_id,
            reason=sig.reason,
            ref_price=mark,
        )
        if self.is_live_mode():
            # Real money: the venue round-trip runs asynchronously; the signal record is completed
            # in _on_entry_result. One entry in flight per symbol.
            if sig.symbol in self._pending_entries:
                return reject(
                    Decision.REJECTED_RISK.value, "live entry already pending on this symbol"
                )
            self._pending_entries.add(sig.symbol)
            record["live"] = {**record.get("live", {}), "contracts": intent.contracts, "stop": stop}
            self._spawn(self._live_entry(intent, sig, record, sizing, entry, stop, cv))
            return None
        res = self.gateway.submit(intent, contract_value=cv, now_us=int(now * 1e6))
        self._on_entry_result(res, sig, record, sizing, entry, stop, cv)

    async def _live_entry(
        self,
        intent: OrderIntent,
        sig: Signal,
        record: dict[str, Any],
        sizing: Any,
        entry: float,
        stop: float,
        cv: float,
    ) -> None:
        try:
            res = await self.gateway.submit_live(
                intent, contract_value=cv, ctx=self.live_context(sig.symbol), stop_price=stop
            )
        finally:
            self._pending_entries.discard(sig.symbol)
        # The venue holds the bracket stop at `stop`; the local stop mirrors it exactly.
        self._on_entry_result(res, sig, record, sizing, entry, stop, cv, venue_stop=stop)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._live_tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self._live_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self.counters["live_task_errors"] += 1
                log.error("live_task_failed", error=repr(t.exception()))

        task.add_done_callback(_done)

    def _on_entry_result(
        self,
        res: OrderResult,
        sig: Signal,
        record: dict[str, Any],
        sizing: Any,
        entry: float,
        stop: float,
        cv: float,
        *,
        venue_stop: float | None = None,
    ) -> None:
        key = sig.strategy.value
        wallet = self.wallets[key]
        record.update(
            decision=res.decision.value, decision_reason=res.order.note, order_id=res.order.id
        )
        self.recent_signals.appendleft(record)
        self._persist(
            self.ledger.insert_signal(
                sig.signal_id,
                key,
                sig.symbol,
                sig.side.value,
                sig.ts,
                res.decision.value,
                res.order.note,
                record,
            )
        )
        self._persist(self.ledger.insert_order(to_json(res.order)))
        self.counters[f"entry_{res.decision.value}"] += 1
        if not res.filled or res.fill is None:
            log.info(
                "entry_not_filled",
                signal_id=sig.signal_id,
                decision=res.decision.value,
                note=res.order.note,
            )
            return
        fill = res.fill
        dist = abs(entry - stop)
        side = PosSide.LONG if sig.side is Side.LONG else PosSide.SHORT
        if venue_stop is not None:
            actual_stop = venue_stop
        else:
            actual_stop = fill.price - dist if side is PosSide.LONG else fill.price + dist
        pos = Position(
            id=new_id("pos"),
            strategy=key,
            symbol=sig.symbol,
            side=side,
            contracts=fill.contracts,
            entry=fill.price,
            stop=actual_stop,
            initial_stop=actual_stop,
            opened_ts=fill.ts,
            signal_id=sig.signal_id,
            contract_value=cv,
            r_unit=abs(fill.price - actual_stop) or dist,
            notional=fill.price * fill.contracts * cv,
            leverage=self.live_caps.leverage if venue_stop is not None else sizing.leverage,
            fees=fill.fee,
        )
        self.positions[pos.id] = pos
        wallet.apply_fee(fill.fee, fill.ts)
        self._persist(self.ledger.upsert_position(to_json(pos)))
        self._persist(self.ledger.upsert_wallet(key, wallet.to_json()))
        self._persist(
            self.ledger.event(
                "entry",
                {
                    "position_id": pos.id,
                    "symbol": pos.symbol,
                    "side": side.value,
                    "contracts": pos.contracts,
                    "entry": pos.entry,
                    "stop": pos.stop,
                    "slippage_bps": fill.slippage_bps,
                    "fee": fill.fee,
                },
            )
        )
        self._persist(
            self.telegram.send(
                f"{self._mode().value} ENTRY {side.value} {pos.contracts} {pos.symbol} @ {pos.entry:.6g} stop {pos.stop:.6g} ({sig.reason})"
            )
        )
        log.info(
            "entry_filled",
            position_id=pos.id,
            symbol=pos.symbol,
            side=side.value,
            contracts=pos.contracts,
            entry=pos.entry,
            stop=pos.stop,
            slippage_bps=round(fill.slippage_bps or 0, 2),
            fee=round(fill.fee, 4),
        )

    # ---- exits -----------------------------------------------------------------------------------
    def _check_stops(self, symbol: str, mark: float, now: float) -> None:
        for pos in [p for p in self.positions.values() if p.symbol == symbol]:
            before = pos.stop
            decision = self.exits.on_mark(pos, mark, now)
            if decision is not None:
                self._close_position(pos, decision, now)
            elif pos.stop != before:
                self.counters["ratchets"] += 1
                self._persist(self.ledger.upsert_position(to_json(pos)))

    def _flatten_all(self, reason: ExitReason, note: str) -> None:
        now = time.time()
        for pos in list(self.positions.values()):
            mark = self.marks.get(pos.symbol) or self.last_price.get(pos.symbol) or pos.entry
            self._close_position(pos, ExitDecision(pos.id, reason, mark, note), now)

    def _close_position(self, pos: Position, decision: ExitDecision, now: float) -> None:
        if pos.status == "CLOSING":
            return  # a live exit is already in flight for this position
        intent = OrderIntent(
            strategy=pos.strategy,
            symbol=pos.symbol,
            side=OrderSide.SELL if pos.side is PosSide.LONG else OrderSide.BUY,
            contracts=pos.contracts,
            purpose=Purpose.EXIT,
            signal_id=pos.signal_id,
            client_order_id=f"{pos.id}-x"[:32],
            reason=f"{decision.reason.value}: {decision.note}",
            position_id=pos.id,
            ref_price=decision.ref_price,
        )
        if self.is_live_mode():
            pos.status = "CLOSING"
            self._spawn(self._live_exit(pos, intent, decision))
            return
        res = self.gateway.submit(intent, contract_value=pos.contract_value, now_us=int(now * 1e6))
        self._on_exit_result(pos, decision, res, now)

    async def _live_exit(self, pos: Position, intent: OrderIntent, decision: ExitDecision) -> None:
        res = await self.gateway.submit_live(
            intent,
            contract_value=pos.contract_value,
            ctx=self.live_context(pos.symbol),
            position=pos,
        )
        if not res.filled and pos.id in self.positions:
            # Exit failed: the position is still open on the venue (and its bracket stop was cancelled
            # if we got that far), so keep it OPEN locally and let the next stop check / reconcile retry.
            pos.status = "OPEN"
            self._persist(
                self.telegram.send(
                    f"LIVE EXIT FAILED {pos.symbol} ({decision.reason.value}): {res.order.note} — "
                    "position still open, will retry"
                )
            )
        self._on_exit_result(pos, decision, res, time.time())

    def _on_exit_result(
        self, pos: Position, decision: ExitDecision, res: OrderResult, now: float
    ) -> None:
        self._persist(self.ledger.insert_order(to_json(res.order)))
        if not res.filled or res.fill is None:
            self.counters["exit_failed"] += 1
            log.error(
                "exit_not_filled",
                position_id=pos.id,
                decision=res.decision.value,
                note=res.order.note,
            )
            return
        fill = res.fill
        wallet = self.wallets[pos.strategy]
        pnl = pos.unrealized(fill.price)
        pos.fees += fill.fee
        pos.status, pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl = (
            "CLOSED",
            fill.ts,
            fill.price,
            decision.reason.value,
            pnl,
        )
        wallet.apply_fee(fill.fee, now)
        wallet.apply_close(pnl, now)
        net = pnl - pos.fees - pos.funding
        r_mult = (fill.price - pos.entry) * pos.direction / pos.r_unit if pos.r_unit else 0.0
        trade = Trade(
            id=new_id("trd"),
            position_id=pos.id,
            strategy=pos.strategy,
            symbol=pos.symbol,
            side=pos.side,
            contracts=pos.contracts,
            entry=pos.entry,
            exit=fill.price,
            pnl=pnl,
            fees=pos.fees,
            funding=pos.funding,
            net=net,
            r_multiple=r_mult,
            mfe_r=pos.mfe_r,
            mae_r=pos.mae_r,
            exit_reason=decision.reason.value,
            opened_ts=pos.opened_ts,
            closed_ts=fill.ts,
            duration_s=fill.ts - pos.opened_ts,
            signal_id=pos.signal_id,
        )
        del self.positions[pos.id]
        self.funding_applied.pop(pos.id, None)
        self.counters[f"exit_{decision.reason.value}"] += 1
        self._persist(self.ledger.upsert_position(to_json(pos)))
        self._persist(self.ledger.insert_trade(to_json(trade)))
        self._persist(self.ledger.upsert_wallet(pos.strategy, wallet.to_json()))
        self._persist(
            self.ledger.event(
                "exit",
                {"position_id": pos.id, "reason": decision.reason.value, "net": net, "r": r_mult},
            )
        )
        self._persist(
            self.telegram.send(
                f"{self._mode().value} EXIT {pos.side.value} {pos.contracts} {pos.symbol} @ {fill.price:.6g} {decision.reason.value} net {net:+.2f} ({r_mult:+.2f}R) bal {wallet.balance:.2f}"
            )
        )
        log.info(
            "exit_filled",
            position_id=pos.id,
            symbol=pos.symbol,
            reason=decision.reason.value,
            exit=fill.price,
            net=round(net, 4),
            r=round(r_mult, 3),
            balance=round(wallet.balance, 2),
        )
        tripped = wallet.check_breakers(self.limits, now)
        if tripped:
            self.counters["breaker_trips"] += 1
            self._persist(
                self.ledger.event("breaker", {"strategy": pos.strategy, "reason": tripped})
            )
            self._persist(self.telegram.send(f"BREAKER {pos.strategy}: {tripped}"))
            log.warning("breaker_tripped", strategy=pos.strategy, reason=tripped)

    # ---- clock -------------------------------------------------------------------------------------
    async def _clock(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
                break
            except TimeoutError:
                pass
            try:
                self._tick(time.time())
            except Exception:
                self.counters["clock_errors"] += 1
                log.exception("clock_failed")

    def _tick(self, now: float) -> None:
        for sym, tb in self.trade_builders.items():
            bars = tb.flush(now)
            if bars:
                self._on_1m_bars(sym, bars, now)
        for pos in list(self.positions.values()):
            ref = self.marks.get(pos.symbol) or self.last_price.get(pos.symbol)
            if ref is None:
                continue
            d = self.exits.on_clock(pos, ref, now)
            if d is not None:
                self._close_position(pos, d, now)
        if self.is_live_mode():
            # Funding is settled by the venue and lands via the wallet sync (reconcile); accruing it
            # locally too would double count. Arming: expiry drops to PAPER and flattens nothing.
            if not self.live_armed(now):
                self.control = {**self.control, "mode": Mode.PAPER.value, "armed_until": None}
                self._persist(self._disarm("arm expired"))
        else:
            self._apply_funding(now)
        for key, w in self.wallets.items():
            if w.rollover(now):
                self._persist(self.ledger.upsert_wallet(key, w.to_json()))
                self._persist(self.ledger.event("rollover", {"strategy": key, "day": w.day}))
        if now - self._last_rest_check >= 300 and self.ws.connected:
            self._last_rest_check = now
            self._persist(self.verify_against_rest())
        hour_key = time.strftime("%Y-%m-%dT%H", time.gmtime(now))
        if hour_key != self._last_hour_key:
            self._last_hour_key = hour_key
            for key, w in self.wallets.items():
                self._persist(self.ledger.snapshot_wallet(key, w.to_json(), now))
            self._persist(self.ledger.insert_health(self.snapshot(brief=True)))

    def _apply_funding(self, now: float) -> None:
        now_us = int(now * 1e6)
        for pos in self.positions.values():
            f = self.funding.get(pos.symbol)
            if (
                f is None
                or now_us < f.next_ts_us
                or self.funding_applied.get(pos.id) == f.next_ts_us
            ):
                continue
            mark = self.marks.get(pos.symbol) or pos.entry
            amount = pos.contracts * pos.contract_value * mark * f.rate_pct / 100 * pos.direction
            pos.funding += amount
            self.wallets[pos.strategy].apply_funding(amount, now)
            self.funding_applied[pos.id] = f.next_ts_us
            self.counters["funding_events"] += 1
            self._persist(
                self.ledger.event(
                    "funding",
                    {
                        "position_id": pos.id,
                        "symbol": pos.symbol,
                        "rate_pct": f.rate_pct,
                        "amount": amount,
                    },
                )
            )
            self._persist(self.ledger.upsert_position(to_json(pos)))
            self._persist(
                self.ledger.upsert_wallet(pos.strategy, self.wallets[pos.strategy].to_json())
            )

    # ---- persistence queue -----------------------------------------------------------------------
    def _persist(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            self._db_queue.put_nowait(coro)
        except asyncio.QueueFull:
            coro.close()
            self.counters["db_queue_dropped"] += 1
            log.error("db_queue_full")

    async def _db_writer(self) -> None:
        while not self.stop_event.is_set():
            coro = await self._db_queue.get()
            try:
                await coro
            except Exception:
                self.counters["db_errors"] += 1
                log.exception("db_write_failed")

    # ---- views -------------------------------------------------------------------------------------
    def forming_bar(self, symbol: str, tf: str) -> UnifiedBar | None:
        tb = self.trade_builders[symbol].current()
        f1 = (
            merge_1m(
                tb,
                None,
                oi=self.oi.get(symbol),
                mark_close=self.marks.get(symbol),
                source="forming",
            )
            if tb
            else None
        )
        return self.store.forming(symbol, tf, f1)

    def micro_view(self, symbol: str, levels: int = 10) -> dict[str, Any]:
        book = self.books[symbol]
        now_us = int(time.time() * 1e6)

        def ladder(side: list[Any]) -> list[dict[str, float]]:
            out, cum = [], 0
            for lv in side[:levels]:
                cum += lv.size
                out.append({"price": lv.price, "size": lv.size, "cum": cum})
            return out

        bars = self.store.bars(symbol, "1m", 15)

        def roll(n: int) -> dict[str, float | None]:
            xs = bars[-n:]
            vol = sum(b.volume for b in xs)
            buy = sum(b.buy_volume for b in xs)
            return {
                "ofi": sum(b.ofi or 0.0 for b in xs if b.has_book),
                "buy_volume": buy,
                "sell_volume": sum(b.sell_volume for b in xs),
                "buy_ratio": buy / vol if vol else None,
                "volume": vol,
                "trades": sum(b.trade_count for b in xs),
                "imbalance": (
                    sum(b.depth_imbalance or 0.0 for b in xs if b.depth_imbalance is not None)
                    / max(1, sum(1 for b in xs if b.depth_imbalance is not None))
                )
                if any(b.depth_imbalance is not None for b in xs)
                else None,
            }

        forming = self.trade_builders[symbol].current()
        return {
            "symbol": symbol,
            "ts": now_us / 1e6,
            "book": {
                "bids": ladder(book.bids),
                "asks": ladder(book.asks),
                "age_ms": book.age_ms(now_us),
                "updates": book.updates,
            },
            "quotes": {
                "bid": book.best_bid.price if book.best_bid else None,
                "bid_size": book.best_bid.size if book.best_bid else None,
                "ask": book.best_ask.price if book.best_ask else None,
                "ask_size": book.best_ask.size if book.best_ask else None,
                "mid": book.mid,
                "microprice": book.microprice,
                "spread_bps": book.spread_bps,
                "imbalance5": book.imbalance(5),
                "imbalance10": book.imbalance(10),
                "depth_bid10": book.depth("bid", 10),
                "depth_ask10": book.depth("ask", 10),
            },
            "mark": self.marks.get(symbol),
            "spot": self.spot.get(symbol),
            "last": self.last_price.get(symbol),
            "oi": self.oi.get(symbol),
            "funding": {
                "rate_pct": self.funding[symbol].rate_pct,
                "next_ts": self.funding[symbol].next_ts_us / 1e6,
            }
            if symbol in self.funding
            else None,
            "rolling": {"1m": roll(1), "5m": roll(5), "15m": roll(15)},
            "forming_1m": {
                "ts": forming.ts,
                "open": forming.open,
                "high": forming.high,
                "low": forming.low,
                "close": forming.close,
                "volume": forming.volume,
                "buy_volume": forming.buy_volume,
                "sell_volume": forming.sell_volume,
                "trade_count": forming.trade_count,
            }
            if forming
            else None,
            "micro": (
                {
                    "kyle_lambda_bps_per_1k": last.kyle_lambda_bps_per_1k,
                    "kyle_r2": last.kyle_r2,
                    "kyle_lambda_15m_bps_per_1k": last.kyle_lambda_15m_bps_per_1k,
                    "vpin": last.vpin,
                    "vpin_fast": last.vpin_fast,
                    "ofi_l5": last.ofi_l5,
                    "ofi_l5_norm": last.ofi_l5_norm,
                    "realized_vol_bps": last.realized_vol_bps,
                    "trade_intensity": last.trade_intensity,
                    "large_trade_share": last.large_trade_share,
                    "max_run": last.max_run,
                    "daily_volume": self.micro_builders[symbol].daily_volume,
                }
                if (last := next((b for b in reversed(bars) if b.has_micro), None))
                else None
            ),
            "recent_1m": [
                {
                    "ts": b.ts,
                    "close": b.close,
                    "volume": b.volume,
                    "buy_volume": b.buy_volume,
                    "sell_volume": b.sell_volume,
                    "trade_count": b.trade_count,
                    "ofi": b.ofi,
                    "microprice": b.microprice,
                    "spread_bps": b.spread_bps,
                    "imbalance": b.depth_imbalance,
                    "vwap": b.vwap,
                    "kyle": b.kyle_lambda_bps_per_1k,
                    "vpin": b.vpin_fast,
                    "rv": b.realized_vol_bps,
                    "ofi_l5": b.ofi_l5_norm,
                    "source": b.source,
                }
                for b in bars
            ],
        }

    def position_view(self, pos: Position) -> dict[str, Any]:
        mark = self.marks.get(pos.symbol) or self.last_price.get(pos.symbol)
        d = to_json(pos)
        d.update(
            mark=mark,
            unrealized=pos.unrealized(mark) if mark else None,
            r_now=pos.r_now(mark) if mark else None,
            age_s=round(time.time() - pos.opened_ts),
        )
        return d

    def snapshot(self, *, brief: bool = False) -> dict[str, Any]:
        now = time.time()
        now_us = int(now * 1e6)
        books = {
            s: {
                "age_ms": b.age_ms(now_us),
                "bid": b.best_bid.price if b.best_bid else None,
                "ask": b.best_ask.price if b.best_ask else None,
                "spread_bps": round(b.spread_bps, 3) if b.spread_bps else None,
                "updates": b.updates,
            }
            for s, b in self.books.items()
        }
        bars = {
            s: {
                "counts": self.store.counts()[s],
                "last_1m_ts": self.last_bar_ts.get(s),
                "last_1m_age_s": round(now - self.last_bar_ts[s] - 60)
                if s in self.last_bar_ts
                else None,
            }
            for s in self.symbols
        }
        snap: dict[str, Any] = {
            "ts": now,
            "uptime_s": round(now - self.started_ts),
            "control": self.control,
            "delta_env": self.settings.delta_env.value,
            "system_status": self.system_status,
            "feed": self.ws.stats(now),
            "books": books,
            "bars": bars,
            "marks": self.marks,
            "funding": {
                s: {"rate_pct": f.rate_pct, "next_in_s": round(f.next_ts_us / 1e6 - now)}
                for s, f in self.funding.items()
            },
            "oi": self.oi,
            "candle_check": {
                k: (list(v) if isinstance(v, deque) else v) for k, v in self.candle_check.items()
            },
            "rest_check": {
                k: (list(v) if isinstance(v, deque) else v) for k, v in self.rest_check.items()
            },
            "archive": self.archive.stats(),
            "gateway": self.gateway.stats(),
            "rate_budget": {"used": self.rest.budget.used(), "quota": self.rest.budget.quota},
            "counters": dict(self.counters),
            "late_trades": {s: tb.late_trades for s, tb in self.trade_builders.items()},
            "wallets": {
                k: w.to_json()
                | {"day_pnl": round(w.day_pnl, 4), "drawdown_pct": round(w.drawdown_pct, 4)}
                for k, w in self.wallets.items()
            },
            "positions": [self.position_view(p) for p in self.positions.values()],
            "db_queue": self._db_queue.qsize(),
            "committee": self.committee.status(),
            "armed": self.live_armed(now),
            "live": self.live.status() if self.live else None,
            "live_ws": self.live_ws.stats(now) if self.live_ws else None,
            "reconcile": self.reconciler.status(),
            "pending_entries": sorted(self._pending_entries),
        }
        if brief:
            snap["candle_check"] = {k: v for k, v in self.candle_check.items() if k != "examples"}
            snap["rest_check"] = {k: v for k, v in self.rest_check.items() if k != "examples"}
        return snap
