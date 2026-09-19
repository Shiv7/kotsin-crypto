"""Decision log with deferred, volatility-adjusted grading (TradingAgents' memory log + Trading-R1's
label scheme). Entries are pending until the horizon elapses; then the realised return is normalised
by the realised volatility of the same window, graded on the 5-class scale, scored ordinally against
the committee's rating, and a short reflection is attached. Recent same-symbol decisions and
cross-symbol lessons are injected into the next run's prompt (point-in-time: only resolved entries)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .labels import ordinal_score, z_label
from .schemas import Rating


class DecisionLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        if path.exists():
            try:
                self.entries = json.loads(path.read_text())
            except (OSError, ValueError):
                self.entries = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, default=str))
        tmp.replace(self.path)

    def append(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        self._save()

    def pending(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e.get("pending")]

    def latest(self, symbol: str) -> dict[str, Any] | None:
        for e in reversed(self.entries):
            if e["symbol"] == symbol:
                return e
        return None

    def resolve(
        self,
        entry_id: str,
        *,
        mark_now: float,
        realized_pct: float,
        z: float,
        reflection: str | None,
        thesis_supported: bool | None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        e = next((x for x in self.entries if x["id"] == entry_id), None)
        if e is None:
            return None
        truth = z_label(z)
        e.update(
            pending=False,
            resolved_ts=now or time.time(),
            mark_at_resolution=mark_now,
            realized_pct=realized_pct,
            z=z,
            truth=truth.value,
            score=ordinal_score(Rating(e["rating"]), truth),
            reflection=reflection,
            thesis_supported=thesis_supported,
        )
        self._save()
        return e

    def past_context(self, symbol: str, n_same: int = 3, n_cross: int = 2) -> str:
        resolved = [e for e in self.entries if not e.get("pending")]
        same = [e for e in reversed(resolved) if e["symbol"] == symbol][:n_same]
        cross = [e for e in reversed(resolved) if e["symbol"] != symbol and e.get("reflection")][
            :n_cross
        ]
        parts = []
        if same:
            parts.append(f"Past committee decisions on {symbol} (most recent first):")
            for e in same:
                parts.append(
                    f"- {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(e['ts']))}: {e['rating']} (conviction {e['conviction']:.2f}) → realised {e['realized_pct']:+.2f}% over {e['horizon_h']}h, z {e['z']:+.2f}, graded {e['truth']}, score {e['score']:.2f}. Lesson: {e.get('reflection') or 'none recorded'}"
                )
        if cross:
            parts.append("Recent lessons from other symbols:")
            for e in cross:
                parts.append(
                    f"- {e['symbol']} {e['rating']} → {e['truth']} (score {e['score']:.2f}): {e['reflection']}"
                )
        return "\n".join(parts)

    def stats(self) -> dict[str, Any]:
        resolved = [e for e in self.entries if not e.get("pending")]
        scores = [e["score"] for e in resolved if e.get("score") is not None]
        by_rating: dict[str, list[float]] = {}
        for e in resolved:
            by_rating.setdefault(e["rating"], []).append(e["score"])
        return {
            "entries": len(self.entries),
            "pending": len(self.entries) - len(resolved),
            "resolved": len(resolved),
            "mean_score": sum(scores) / len(scores) if scores else None,
            "mean_score_by_rating": {k: sum(v) / len(v) for k, v in by_rating.items()},
            "hit_rate_direction": (
                sum(1 for e in resolved if e.get("thesis_supported")) / len(resolved)
            )
            if resolved
            else None,
        }
