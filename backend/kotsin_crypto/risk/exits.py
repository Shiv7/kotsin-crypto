"""The ONLY module that decides an exit (R11). Evaluated on every mark-price tick and every closed bar:
initial stop → R-ladder ratchet → trail; time-stop as the backstop. The stop only ever tightens."""

from __future__ import annotations

from ..domain import ExitDecision, ExitReason, Position, PosSide
from .limits import RiskLimits


class ExitEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def on_mark(self, pos: Position, mark: float, now: float) -> ExitDecision | None:
        """Update excursions and the ratchet; return a decision when the stop is hit."""
        if pos.status != "OPEN" or pos.r_unit <= 0:
            return None
        r = pos.r_now(mark)
        pos.mfe_r = max(pos.mfe_r, r)
        pos.mae_r = min(pos.mae_r, r)
        if r > pos.peak_r:
            pos.peak_r = r
            self._ratchet(pos)
        hit = mark <= pos.stop if pos.side is PosSide.LONG else mark >= pos.stop
        if hit:
            return ExitDecision(
                pos.id,
                ExitReason.STOP,
                mark,
                note=f"stop {pos.stop:.6g} hit at mark {mark:.6g}, peak {pos.peak_r:.2f}R",
            )
        return None

    def on_clock(self, pos: Position, mark: float, now: float) -> ExitDecision | None:
        if pos.status != "OPEN":
            return None
        if now - pos.opened_ts >= self.limits.time_stop_s:
            return ExitDecision(
                pos.id, ExitReason.TIME_STOP, mark, note=f"held {int(now - pos.opened_ts)}s"
            )
        return None

    def _ratchet(self, pos: Position) -> None:
        lock_r: float | None = None
        for trigger, lock in self.limits.ratchet:
            if pos.peak_r >= trigger:
                lock_r = lock
        top_trigger = self.limits.ratchet[-1][0]
        if pos.peak_r > top_trigger:
            lock_r = max(lock_r or 0.0, pos.peak_r - self.limits.trail_r_after_ladder)
        if lock_r is None:
            return
        new_stop = pos.entry + pos.direction * lock_r * pos.r_unit
        if (pos.side is PosSide.LONG and new_stop > pos.stop) or (
            pos.side is PosSide.SHORT and new_stop < pos.stop
        ):
            pos.stop = new_stop
