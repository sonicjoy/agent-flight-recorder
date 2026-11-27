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

from flight_recorder.analyst.items import graded_items
from flight_recorder.evals.cases import CaseError, Draft, draft_case, problems, write_case
from flight_recorder.evals.checks import same
from flight_recorder.payloads import none_as_null
from flight_recorder.redact import Redactor
from flight_recorder.store.labels import decided_fields
from flight_recorder.store.models import Run

LOG_DIR = Path("evals/analyst-log")
LOG_VERSION = "1"


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


def expects_a_wrong_value(session: Session, run_id: str, draft: Draft) -> str | None:
    """The first field (by name) whose expected value is the agent's decision that the run's
    recorded truth marks wrong, whichever group the run is written for; None when there is
    none. `afr label --answer` keeps the agent's decision for every field an answer leaves
    out, so an answer that fixed one field can still expect another field's mistake."""
    run = session.get(Run, uuid.UUID(run_id))
    if run is None:
        return None
    attributes = dict(run.attributes or {})
    decided = decided_fields(attributes)
    expected = draft.case["expected"]
    wrong = sorted(item["id"].split("#", 1)[1] for item in graded_items(run_id, "", attributes))
    for name in wrong:
        if (
            name in expected
            and name in decided
            and same(expected[name], none_as_null(decided[name]))
        ):
            return name
    return None


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
            try:
                exists = draft.path(root).exists()
            except CaseError as error:
                raise WriteFailure(str(error), written) from error
            if exists:
                written.skipped.append((run_id, "a case for this run exists"))
            elif problems(draft.case):
                written.skipped.append((run_id, "it holds redacted values; use afr case new"))
            elif not draft.case["checks"]:
                written.skipped.append((run_id, "it has nothing to check"))
            elif wrong := expects_a_wrong_value(session, run_id, draft):
                written.skipped.append((run_id, f"its case would expect the agent's {wrong}, "
                                        "which the recorded truth marks wrong: correct the "
                                        "answer with afr label --answer, or label the run "
                                        "debatable-truth if the agent was right"))  # fmt: skip
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
    sha256 of its text (so #23 can tell a case kept as written from an edited one). Never
    overwrites an earlier log; the folder carries its own `.gitignore`, as logs name runs from
    the store wherever afr runs. Raises OSError when the OS refuses."""
    log_dir.mkdir(parents=True, exist_ok=True)
    ignore = log_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# afr analyse's decisions logs name runs from your store\n*\n",
                          encoding="utf-8", newline="\n")  # fmt: skip
    record = {
        "version": LOG_VERSION,
        "params": params,
        "groups": [g | {"kept": n in kept} for n, g in enumerate(groups, 1)],
        "written": [{"path": p.as_posix(),
                     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in written.paths],
        "skipped": [{"run": run, "reason": reason} for run, reason in written.skipped],
    }  # fmt: skip
    stamp = f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}"
    for n in range(100):
        path = log_dir / (f"{stamp}.json" if n == 0 else f"{stamp}-{n}.json")
        try:
            with path.open("x", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, indent=2) + "\n")
        except FileExistsError:
            continue
        return path
    raise FileExistsError(f"too many logs named {stamp} in {log_dir}")
