"""The analyst's decisions log (#22, docs/designs/analyst.md §5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from flight_recorder.analyst.write import Written, write_log

NOW = datetime(2026, 11, 27, 9, 0, 0, tzinfo=UTC)


def test_logs_in_the_same_second_never_overwrite_each_other(tmp_path: Path) -> None:
    paths = [write_log(tmp_path, params={"n": n}, groups=[], kept=set(), written=Written(),
                       now=NOW) for n in range(3)]  # fmt: skip

    assert [p.name for p in paths] == [
        "20261127T090000Z.json", "20261127T090000Z-1.json", "20261127T090000Z-2.json"
    ]  # fmt: skip
    assert [json.loads(p.read_text(encoding="utf-8"))["params"]["n"] for p in paths] == [0, 1, 2]
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"
