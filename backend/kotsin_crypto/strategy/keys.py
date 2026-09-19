"""The single registry of strategy identities (LEARNINGS R6, R8). Strings appear nowhere else."""

from __future__ import annotations

from enum import StrEnum


class StrategyKey(StrEnum):
    CAN2 = "CAN2"  # 5m volume-surge + breakout momentum, ratchet exits
    FUDKII = "FUDKII"  # 30m Bollinger + SuperTrend flip with volume / OI confirmation
    BBSQ = "BBSQ"  # Bollinger squeeze → expansion breakout
