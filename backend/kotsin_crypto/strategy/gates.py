"""Gates with an explicit policy for missing data (LEARNINGS R5).

A gate never guesses: when its input is ``None`` it returns ``passed`` according to ``on_missing`` and
flags ``missing=True`` so the caller can count and alert. ``required=False`` gates contribute evidence
but cannot veto.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum


class OnMissing(StrEnum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    required: bool
    value: float | None
    threshold: float | None
    missing: bool
    note: str = ""


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    on_missing: OnMissing
    required: bool = True

    def evaluate(
        self,
        value: float | None,
        predicate: Callable[[float], bool],
        threshold: float | None = None,
        note: str = "",
    ) -> GateResult:
        if value is None:
            return GateResult(
                name=self.name,
                passed=self.on_missing is OnMissing.FAIL_OPEN,
                required=self.required,
                value=None,
                threshold=threshold,
                missing=True,
                note=f"input missing → {self.on_missing.value}",
            )
        return GateResult(
            name=self.name,
            passed=bool(predicate(value)),
            required=self.required,
            value=value,
            threshold=threshold,
            missing=False,
            note=note,
        )


def chain_passed(results: Iterable[GateResult]) -> bool:
    return all(r.passed or not r.required for r in results)


def failed_gates(results: Iterable[GateResult]) -> list[str]:
    return [r.name for r in results if r.required and not r.passed]
