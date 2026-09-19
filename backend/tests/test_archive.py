from __future__ import annotations

import json
from pathlib import Path

from kotsin_crypto.feed.archive import Archive


def test_archive_appends_jsonl_by_channel_and_hour(tmp_path: Path) -> None:
    a = Archive(tmp_path)
    a.append("trades", '{"p":1}', 1_000)
    a.append("trades", '{"p":2}', 2_000)
    a.append("ticker", '{"x":1}', 3_000)
    assert a.stats()["buffered"] == 3
    assert a.flush() == 3
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.jsonl"))
    assert len(files) == 2 and files[0].startswith("ticker/") and files[1].startswith("trades/")
    lines = [json.loads(line) for line in (tmp_path / files[1]).read_text().splitlines()]
    assert lines == [{"t": 1000, "m": {"p": 1}}, {"t": 2000, "m": {"p": 2}}]
    a.close()
    assert a.stats()["rows"] == {"trades": 2, "ticker": 1} and a.stats()["errors"] == 0
