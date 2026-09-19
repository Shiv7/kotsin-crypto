from __future__ import annotations

from kotsin_crypto.strategy.gates import Gate, OnMissing, chain_passed, failed_gates


def test_missing_input_follows_declared_policy() -> None:
    open_gate = Gate("oi", OnMissing.FAIL_OPEN)
    closed_gate = Gate("volume", OnMissing.FAIL_CLOSED)
    r_open = open_gate.evaluate(None, lambda v: v > 1)
    r_closed = closed_gate.evaluate(None, lambda v: v > 1)
    assert r_open.passed and r_open.missing
    assert not r_closed.passed and r_closed.missing


def test_chain_semantics() -> None:
    required = Gate("trend", OnMissing.FAIL_CLOSED).evaluate(0.2, lambda v: v > 0.5, threshold=0.5)
    optional = Gate("vwap", OnMissing.FAIL_CLOSED, required=False).evaluate(0.2, lambda v: v > 0.5)
    ok = Gate("rr", OnMissing.FAIL_CLOSED).evaluate(2.0, lambda v: v >= 1.5, threshold=1.5)
    assert not chain_passed([required, optional, ok])
    assert failed_gates([required, optional, ok]) == ["trend"]
    assert chain_passed([optional, ok])
