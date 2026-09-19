"""The committee as a service: scheduled per-symbol runs, deferred grading, and an advisory size
multiplier. Structurally unable to block or delay a trade — it runs on its own task, the engine only
reads its latest decision, and its influence on sizing is off by default and floored at 0.5×."""

from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from ..config import Settings
from ..domain import new_id
from .evidence import build_pack, render
from .llm import LLM, AnthropicLLM
from .memory import DecisionLog
from .pipeline import reflect, run_committee
from .schemas import Rating, agreement_multiplier

if TYPE_CHECKING:
    from ..engine import Engine

log = structlog.get_logger("committee")


class CommitteeService:
    def __init__(self, engine: Engine, settings: Settings, llm: LLM | None = None) -> None:
        self.engine = engine
        self.settings = settings
        key = (
            settings.anthropic_api_key.get_secret_value()
            if settings.anthropic_api_key
            else os.environ.get("ANTHROPIC_API_KEY") or None
        )
        self.llm: LLM | None = llm or (AnthropicLLM(key, settings.committee_model) if key else None)
        self.available = self.llm is not None
        self.scheduled = self.available and settings.committee_enabled
        self.log = DecisionLog(settings.data_dir / "committee" / "decisions.json")
        self.latest: dict[str, dict[str, Any]] = {}
        for sym in engine.symbols:
            e = self.log.latest(sym)
            if e:
                self.latest[sym] = e
        self.running: set[str] = set()
        self.runs_today = 0
        self._day = self._today()
        self.errors = 0
        self.last_error = ""
        self.last_run_ts: float | None = None

    @staticmethod
    def _today() -> str:
        return datetime.now(tz=UTC).strftime("%Y-%m-%d")

    # ---- scheduling ------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
                break
            except TimeoutError:
                pass
            if self._today() != self._day:
                self._day, self.runs_today = self._today(), 0
            try:
                await self.resolve_due()
            except Exception:
                log.exception("committee_resolve_failed")
            if not self.scheduled:
                continue
            for sym in self.engine.symbols:
                if (
                    self._due(sym)
                    and self.runs_today < self.settings.committee_max_runs_per_day
                    and self.engine.ws.connected
                ):
                    await self.run_symbol(sym)

    def _due(self, symbol: str) -> bool:
        e = self.latest.get(symbol)
        return e is None or time.time() - e["ts"] >= self.settings.committee_interval_h * 3600

    async def run_symbol(self, symbol: str) -> dict[str, Any]:
        if self.llm is None:
            raise RuntimeError("committee unavailable: no KC_ANTHROPIC_API_KEY")
        if symbol in self.running:
            raise RuntimeError(f"a run for {symbol} is already in progress")
        self.running.add(symbol)
        try:
            pack = await build_pack(self.engine, symbol)
            text = render(pack)
            book = (
                "\n".join(
                    f"- {p['side']} {p['contracts']} {p['symbol']} entry {p['entry']} ({p['r_now']:+.2f}R)"
                    for p in pack.get("book.open_positions", [])
                )
                or "flat"
            )
            run = await run_committee(
                self.llm,
                symbol=symbol,
                evidence_text=text,
                past_context=self.log.past_context(symbol),
                portfolio_text=f"wallet ${pack.get('book.wallet_balance')}, day P&L {pack.get('book.wallet_day_pnl_pct')}%\n{book}",
                horizon_h=self.settings.committee_horizon_h,
            )
            self.runs_today += 1
            self.last_run_ts = time.time()
            if run.error or run.decision is None:
                self.errors += 1
                self.last_error = run.error or "no decision"
                log.error("committee_run_failed", symbol=symbol, error=self.last_error)
                entry = {
                    "id": new_id("cmt"),
                    "symbol": symbol,
                    "ts": run.started_ts,
                    "error": self.last_error,
                    "pending": False,
                    "run": run.to_json(),
                }
                self.log.append(entry)
                return entry
            d = run.decision
            entry = {
                "id": new_id("cmt"),
                "symbol": symbol,
                "ts": run.started_ts,
                "rating": d.rating.value,
                "conviction": d.conviction,
                "size_multiplier": d.size_multiplier,
                "horizon_h": min(d.horizon_h, 72) or self.settings.committee_horizon_h,
                "review": d.review,
                "mark_at_decision": self.engine.marks.get(symbol)
                or self.engine.last_price.get(symbol),
                "thesis": {
                    "market": d.thesis_market,
                    "flow": d.thesis_flow,
                    "derivatives": d.thesis_derivatives,
                    "risk": d.thesis_risk,
                },
                "invalidation": d.invalidation,
                "evidence_keys": d.evidence_keys,
                "pack": {k: v for k, v in pack.items() if k != "bars_5m"},
                "run": run.to_json(),
                "pending": not d.review and entry_mark_ok(self.engine, symbol),
            }
            self.log.append(entry)
            self.latest[symbol] = entry
            self.engine._persist(
                self.engine.ledger.event(
                    "committee",
                    {
                        "symbol": symbol,
                        "rating": d.rating.value,
                        "conviction": d.conviction,
                        "size_multiplier": d.size_multiplier,
                        "review": d.review,
                        "calls": run.calls,
                        "seconds": run.seconds,
                    },
                )
            )
            self.engine._persist(
                self.engine.telegram.send(
                    f"COMMITTEE {symbol}: {d.rating.value} (conviction {d.conviction:.2f}, size ×{d.size_multiplier:.2f}, {d.horizon_h}h){' REVIEW' if d.review else ''} — {d.invalidation[:120]}"
                )
            )
            log.info(
                "committee_decision",
                symbol=symbol,
                rating=d.rating.value,
                conviction=d.conviction,
                size=d.size_multiplier,
                review=d.review,
                calls=run.calls,
                seconds=round(run.seconds, 1),
            )
            return entry
        finally:
            self.running.discard(symbol)

    # ---- grading ---------------------------------------------------------------------------------
    async def resolve_due(self) -> int:
        now = time.time()
        n = 0
        for e in self.log.pending():
            if now < e["ts"] + e["horizon_h"] * 3600:
                continue
            sym = e["symbol"]
            mark = self.engine.marks.get(sym) or self.engine.last_price.get(sym)
            m0 = e.get("mark_at_decision")
            if not mark or not m0:
                continue
            bars = [
                b
                for b in self.engine.store.bars(sym, "5m", e["horizon_h"] * 12 + 2)
                if b.ts >= e["ts"]
            ]
            rets = [
                math.log(bars[i].close / bars[i - 1].close)
                for i in range(1, len(bars))
                if bars[i - 1].close
            ]
            if len(rets) >= 6:
                mu = sum(rets) / len(rets)
                vol = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(
                    len(rets)
                )
            else:
                vol = 0.0
            realized = (mark / m0 - 1) * 100
            z = math.log(mark / m0) / vol if vol > 0 else 0.0
            reflection, supported = None, None
            if self.llm is not None:
                try:
                    r = await reflect(
                        self.llm,
                        decision_text=f"{e['rating']} conviction {e['conviction']:.2f}; thesis: {e['thesis']}; invalidation: {e['invalidation']}",
                        realized_pct=realized,
                        z=z,
                        truth=z_label_name(z),
                        horizon_h=e["horizon_h"],
                    )
                    reflection, supported = r.lesson, r.thesis_supported
                except Exception as exc:
                    self.errors += 1
                    self.last_error = f"reflection: {exc}"
            self.log.resolve(
                e["id"],
                mark_now=mark,
                realized_pct=realized,
                z=z,
                reflection=reflection,
                thesis_supported=supported,
                now=now,
            )
            n += 1
            log.info(
                "committee_graded",
                symbol=sym,
                rating=e["rating"],
                realized_pct=round(realized, 3),
                z=round(z, 2),
            )
        return n

    # ---- influence -------------------------------------------------------------------------------
    def size_multiplier(self, symbol: str, side: str) -> tuple[float, str]:
        e = self.latest.get(symbol)
        if not e or e.get("review") or e.get("error"):
            return 1.0, "no fresh committee decision"
        if time.time() - e["ts"] > self.settings.committee_interval_h * 3600 * 1.5:
            return 1.0, "committee decision stale"
        m = agreement_multiplier(Rating(e["rating"]), side)
        m = min(m, max(0.5, float(e.get("size_multiplier", 1.0))))
        return m, f"{e['rating']} conviction {e['conviction']:.2f}"

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "scheduled": self.scheduled,
            "size_influence": self.settings.committee_size_influence,
            "model": self.llm.model if self.llm else None,
            "interval_h": self.settings.committee_interval_h,
            "horizon_h": self.settings.committee_horizon_h,
            "runs_today": self.runs_today,
            "max_runs_per_day": self.settings.committee_max_runs_per_day,
            "running": sorted(self.running),
            "errors": self.errors,
            "last_error": self.last_error,
            "last_run_ts": self.last_run_ts,
            "llm": self.llm.stats() if self.llm else None,
            "log": self.log.stats(),
            "latest": {
                s: {
                    k: e.get(k)
                    for k in (
                        "id",
                        "ts",
                        "rating",
                        "conviction",
                        "size_multiplier",
                        "horizon_h",
                        "review",
                        "pending",
                        "score",
                        "truth",
                        "error",
                    )
                }
                for s, e in self.latest.items()
            },
        }


def entry_mark_ok(engine: Engine, symbol: str) -> bool:
    return bool(engine.marks.get(symbol) or engine.last_price.get(symbol))


def z_label_name(z: float) -> str:
    from .labels import z_label

    return z_label(z).value
