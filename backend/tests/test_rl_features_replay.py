from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from kotsin_crypto.bars.unified import UnifiedBar
from kotsin_crypto.research.features import FEATURE_COLUMNS, build_frame, regime_of, resample_5m
from kotsin_crypto.research.replay import replay, write_bars

T0 = 1_700_000_000 - 1_700_000_000 % 300


def _walk(n: int, seed: int = 0, tf: str = "5m", step: int = 300) -> list[UnifiedBar]:
    rng = np.random.default_rng(seed)
    px = 100.0
    out = []
    for i in range(n):
        o = px
        c = px * float(np.exp(rng.normal(0, 0.002)))
        h, lo = max(o, c) * 1.001, min(o, c) * 0.999
        out.append(
            UnifiedBar(
                symbol="BTCUSD",
                tf=tf,
                ts=T0 + i * step,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=float(rng.integers(50, 500)),
                has_trades=True,
                trade_count=10,
            )
        )
        px = c
    return out


def test_features_are_point_in_time() -> None:
    bars = _walk(400)
    full = build_frame("BTCUSD", bars)
    prefix = build_frame("BTCUSD", bars[:250])
    for c in FEATURE_COLUMNS:
        assert np.allclose(full[c][:250], prefix[c], equal_nan=True), c
    assert np.isnan(full["fwd_ret_12"][-12:]).all()  # targets are the only forward-looking columns
    assert len(full["ts"]) == 400 and full["ts"][0] == T0


def test_regime_labels_are_from_a_fixed_vocabulary() -> None:
    regs = {
        regime_of(
            {
                "rv_48": rv,
                "hour_sin": np.sin(2 * np.pi * h / 24),
                "hour_cos": np.cos(2 * np.pi * h / 24),
            }
        )
        for rv in (0.001, 0.006, 0.02, float("nan"))
        for h in (1, 9, 15, 22)
    }
    vols = {r.split("|")[0] for r in regs}
    sessions = {r.split("|")[1] for r in regs}
    assert vols == {"vol_low", "vol_mid", "vol_high"} and sessions == {"asia", "eu", "us", "late"}


def test_resample_5m_aligns_buckets() -> None:
    b1 = _walk(30, tf="1m", step=60)
    b5 = resample_5m("BTCUSD", b1)
    assert len(b5) == 6 and all(b.ts % 300 == 0 for b in b5) and b5[0].open == b1[0].open


def _write_archive(root: Path) -> None:
    m0 = (T0 // 60) * 60_000_000  # minute start in µs
    rows: dict[str, list[dict]] = {
        "trades": [],
        "ob_l1": [],
        "ob_l2": [],
        "mark_price": [],
        "funding_rate": [],
    }
    px = 100.0
    for minute in range(3):
        for k in range(10):
            t = m0 + minute * 60_000_000 + k * 5_000_000
            px += 0.1 if k % 2 == 0 else -0.05
            rows["trades"].append(
                {
                    "t": t + 300_000,
                    "m": {
                        "type": "trades",
                        "sy": "BTCUSD",
                        "p": f"{px:.2f}",
                        "s": 2.0,
                        "r": "t" if k % 3 else "m",
                        "t": t,
                        "ts": t + 250_000,
                    },
                }
            )
            rows["ob_l1"].append(
                {
                    "t": t + 100_000,
                    "m": {
                        "type": "ob_l1",
                        "sy": "BTCUSD",
                        "bp": f"{px - 0.05:.2f}",
                        "bs": "10",
                        "ap": f"{px + 0.05:.2f}",
                        "as": "8",
                        "lts": t,
                        "ts": t + 50_000,
                    },
                }
            )
            if k % 2 == 0:
                rows["ob_l2"].append(
                    {
                        "t": t + 120_000,
                        "m": {
                            "type": "ob_l2",
                            "sy": "BTCUSD",
                            "b": [[f"{px - 0.05:.2f}", "10"], [f"{px - 0.10:.2f}", "20"]],
                            "a": [[f"{px + 0.05:.2f}", "8"], [f"{px + 0.10:.2f}", "15"]],
                            "lts": t,
                            "ts": t + 60_000,
                        },
                    }
                )
            rows["mark_price"].append(
                {
                    "t": t + 150_000,
                    "m": {
                        "type": "mark_price",
                        "sy": "MARK:BTCUSD",
                        "p": f"{px:.3f}",
                        "ts": t + 70_000,
                    },
                }
            )
        rows["funding_rate"].append(
            {
                "t": m0 + minute * 60_000_000 + 1_000,
                "m": {
                    "type": "funding_rate",
                    "sy": "BTCUSD",
                    "fr": 0.01,
                    "fi": 28800,
                    "nfr": m0 + 8 * 3600_000_000,
                    "ts": m0,
                },
            }
        )
    # a late message so the third minute closes (engine clock emulation)
    rows["mark_price"].append(
        {
            "t": m0 + 3 * 60_000_000 + 2_500_000,
            "m": {
                "type": "mark_price",
                "sy": "MARK:BTCUSD",
                "p": f"{px:.3f}",
                "ts": m0 + 3 * 60_000_000 + 2_000_000,
            },
        }
    )
    for ch, lines in rows.items():
        d = root / ch / "2026-09-19"
        d.mkdir(parents=True)
        with open(d / "20.jsonl", "w") as fh:
            for line in lines:
                fh.write(json.dumps({"t": line["t"], "m": line["m"]}, separators=(",", ":")) + "\n")


def test_replay_rebuilds_bars_with_microstructure(tmp_path: Path) -> None:
    _write_archive(tmp_path / "archive")
    bars = replay(tmp_path / "archive", "BTCUSD", daily_volume=100_000)
    assert len(bars) == 3 and [b.ts for b in bars] == [(T0 // 60) * 60 + i * 60 for i in range(3)]
    b = bars[1]
    assert b.trade_count == 10 and b.volume == 20 and b.has_trades and b.source == "live"
    assert (
        b.has_book
        and b.book_updates == 10
        and b.spread_bps is not None
        and b.microprice is not None
    )
    assert (
        b.has_micro
        and b.ofi_l5 is not None
        and b.has_mark
        and b.has_funding
        and b.funding_rate == 0.01
    )
    assert b.buy_volume == 2.0 * sum(1 for k in range(10) if k % 3) and b.sell_volume == 2.0 * sum(
        1 for k in range(10) if not k % 3
    )


def test_replay_is_deterministic(tmp_path: Path) -> None:
    _write_archive(tmp_path / "archive")
    a = replay(tmp_path / "archive", "BTCUSD")
    b = replay(tmp_path / "archive", "BTCUSD")
    assert [asdict(x) for x in a] == [asdict(x) for x in b]
    write_bars(a, tmp_path / "a.parquet")
    write_bars(b, tmp_path / "b.parquet")
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()
