"""Writing the cases the owner kept (docs/designs/analyst.md §5), and the decisions log.

Every file is written by `write_case`, under the cases folder, with an id from the run
(`run-<8>`): nothing the model said chooses a path. A run that already has a case is skipped,
so a rerun never writes it twice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from flight_recorder.evals.cases import CaseError, draft_case, problems, write_case
from flight_recorder.redact import Redactor

LOG_DIR = Path("evals/analyst-log")


class WriteFailure(Exception):
    """A case couldn't be written for a reason other than the skips; `written` holds the cases
    written before it."""

    def __init__(self, message: str, written: Written) -> None:
        super().__init__(message)
        self.written = written


@dataclass
class Written:
    paths: list[Path] = field(default_factory=list[Path])
    skipped: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """(run id, reason) for each representative not written."""


def write_groups(
    session: Session,
    groups: list[dict[str, Any]],
    run_of: dict[str, str],
    *,
    root: Path,
    suite: str,
    redactor: Redactor,
) -> Written:
    """One case per representative of each group given, tagged with the group's signature
    and name. Raises WriteFailure, carrying what was written, for an unexpected write error."""
    written = Written()
    for group in groups:
        for item_id in group["representatives"]:
            run_id = run_of[item_id]
            try:
                draft = draft_case(session, uuid.UUID(run_id), suite=suite, redactor=redactor)
            except CaseError as error:
                written.skipped.append((run_id, str(error)))
                continue
            tags = list(dict.fromkeys([*draft.case["tags"], group["signature"], group["name"]]))
            draft = draft.with_case(draft.case | {"tags": tags})
            if draft.path(root).exists():
                written.skipped.append((run_id, "a case for this run exists"))
            elif problems(draft.case):
                written.skipped.append((run_id, "it holds redacted values; use afr case new"))
            elif not draft.case["checks"]:
                written.skipped.append((run_id, "it has nothing to check"))
            else:
                try:
                    written.paths.append(write_case(root, draft))
                except CaseError as error:
                    raise WriteFailure(str(error), written) from error
    return written


def write_log(
    log_dir: Path, *, params: dict[str, Any], groups: list[dict[str, Any]], kept: set[int],
    written: Written, now: datetime,
) -> Path:  # fmt: skip
    """The decisions log: parameters, every group with kept or not, each written case with the
    sha256 of its text (so #23 can tell a case kept as written from an edited one)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}.json"
    record = {
        "params": params,
        "groups": [g | {"kept": n in kept} for n, g in enumerate(groups, 1)],
        "written": [{"path": p.as_posix(),
                     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in written.paths],
        "skipped": [{"run": run, "reason": reason} for run, reason in written.skipped],
    }  # fmt: skip
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path
