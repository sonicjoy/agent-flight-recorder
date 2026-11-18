"""Loading a suite, calling its agent, and the files a run leaves (#15, docs/evals/harness.md).

A suite is a folder, `<root>/<suite>/` or `<root>/local/<suite>/`: `suite.yaml` (the agent and
a model label) and one `<id>.yaml` per case. A run calls the agent once per case that isn't
skipped and writes `outputs.jsonl`: one header line, then one redacted line per case. Grading
is `grade.py`'s job; nothing here opens the store.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from importlib.metadata import PackageNotFoundError, packages_distributions, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from flight_recorder.evals import MARK
from flight_recorder.evals.cases import (
    SUFFIX,
    SUITE,
    CaseError,
    check_id,
    load,
    suite_dir,
)
from flight_recorder.evals.checks import canonical, convert, validate
from flight_recorder.evals.grade import input_hash
from flight_recorder.safe_text import strip_fingerprints

if TYPE_CHECKING:
    from flight_recorder.redact import Redactor

SETTING_KEYS = ("agent", "model")
OUTPUTS = "outputs.jsonl"
BASELINE = "baseline.json"
DAMAGED = f"{OUTPUTS} is damaged: run the suite again"


@dataclass(frozen=True)
class Suite:
    name: str
    folder: Path
    agent: str | None  # module:function
    model: str | None  # a free-text label for the report
    cases: list[dict[str, Any]]  # in id order


def _read(path: Path) -> dict[str, Any]:
    try:
        return load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise CaseError(f"{path.name}: can't read it: {error}") from error
    except CaseError as error:
        raise CaseError(f"{path.name}: {error}") from error


def _settings(path: Path) -> dict[str, Any]:
    settings = _read(path)
    for key, value in settings.items():
        if key not in SETTING_KEYS:
            raise CaseError(
                f"{path.name}: unknown key {key!r} (it takes {', '.join(SETTING_KEYS)})"
            )
        if value is not None and not isinstance(value, str):
            raise CaseError(f"{path.name}: {key!r} must be text")
    return settings


def _case(path: Path) -> dict[str, Any]:
    case_id = check_id(path.stem, "case id")
    case = _read(path)
    problems: list[str] = []
    if case.get("id") != case_id:
        problems.append(f"'id' must be {case_id!r}, the file's name")
    if not isinstance(case.get("input"), dict):
        problems.append("'input' must be a mapping")
    else:
        try:
            input_hash(case)
        except ValueError as error:
            problems.append(f"'input' can't be stored as JSON ({error})")
    if "unreviewed" in case and not isinstance(case["unreviewed"], bool):
        problems.append("'unreviewed' must be true or false")
    problems += validate(case.get("checks"))
    if problems:
        raise CaseError(f"{path.name}: " + "; ".join(problems))
    # check values compared in the form outputs are stored in: a YAML date as ISO text
    return case | {"checks": convert(case["checks"])}


def load_suite(root: Path, name: str, *, agent: str | None = None) -> Suite:
    """The suite `name` under `root`, every case validated (unreviewed ones too), so a broken
    case stops the command before any case runs. Raises CaseError."""
    shared, local = suite_dir(root, name, local=False), suite_dir(root, name, local=True)
    if shared.is_dir() and local.is_dir():
        raise CaseError(f"suite {name!r} is in both {shared} and {local}: keep one")
    folder = shared if shared.is_dir() else local if local.is_dir() else None
    if folder is None:
        raise CaseError(f"no suite {name!r} in {root} (or its local folder)")
    settings: dict[str, Any] = {}
    cases: list[dict[str, Any]] = []
    for path in sorted(p for p in folder.iterdir() if p.is_file()):
        if path.suffix.lower() in (".yml", SUFFIX) and path.suffix != SUFFIX:
            raise CaseError(f"{path.name}: use .yaml, so no case is skipped by accident")
        if path.suffix != SUFFIX:
            continue
        if path.stem == SUITE:
            settings = _settings(path)
        else:
            cases.append(_case(path))
    return Suite(name, folder, agent or settings.get("agent"), settings.get("model"), cases)


# --- the agent ------------------------------------------------------------------------


def import_agent(spec: str) -> Callable[[dict[str, Any]], Any]:
    module_name, colon, function = spec.partition(":")
    if not (colon and module_name and function):
        raise CaseError(f"agent {spec!r} must be module:function")
    try:
        module = importlib.import_module(module_name)
    except Exception as error:  # an agent module can fail to import in any way
        raise CaseError(f"can't import {module_name}: {type(error).__name__}: {error}") from error
    agent = getattr(module, function, None)
    if agent is None:
        raise CaseError(f"{module_name} has no {function!r}")
    if not callable(agent):
        raise CaseError(f"{spec} isn't callable")
    # suite.yaml is data: only a function its author marked as an agent may be called, never
    # one a YAML file happens to name (subprocess:run, flight_recorder.store.db:downgrade)
    if getattr(agent, MARK, None) is not True:
        raise CaseError(f"{spec} isn't marked as an agent: decorate it with "
                        "@agent from flight_recorder.evals")  # fmt: skip
    return cast("Callable[[dict[str, Any]], Any]", agent)


@cache
def _distributions() -> Mapping[str, list[str]]:
    return packages_distributions()  # reads every installed distribution: about a second


def agent_version(spec: str) -> str | None:
    """The installed version of the distribution that provides the agent's package, else
    the package's `__version__`: an editable install (`uv sync`) records no package files,
    so `packages_distributions` doesn't know it."""
    package = spec.partition(":")[0].split(".")[0]
    for distribution in _distributions().get(package, []):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    try:
        found = getattr(importlib.import_module(package), "__version__", None)
    except ImportError:
        return None
    return found if isinstance(found, str) else None


def git_sha(folder: Path) -> str | None:
    """The short commit of the repo holding `folder`, `-dirty` when tracked files changed;
    None outside git. GIT_* variables are dropped so a calling hook's repo isn't used."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True,
                                  text=True, env=env, check=False)  # fmt: skip
        except OSError:  # no git
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    sha = git("rev-parse", "--short", "HEAD")
    if not sha:
        return None
    return f"{sha}-dirty" if git("status", "--porcelain", "--untracked-files=no") else sha


def _described(error: BaseException) -> str:
    try:
        return f"{type(error).__name__}: {error}"
    except Exception:  # an exception whose __str__ raises
        return f"{type(error).__name__}: (no message)"


def _call(agent: Callable[[dict[str, Any]], Any], case: dict[str, Any]) -> tuple[Any, str | None]:
    """`(output, error)` for one case. The agent gets a copy of the input as loaded."""
    try:
        answer = agent(copy.deepcopy(case["input"]))
    except KeyboardInterrupt:
        raise
    except BaseException as error:  # arbitrary code (SystemExit, CancelledError...): one case
        return None, _described(error)
    try:
        output = convert(answer)
    except ValueError as error:  # "output not JSON: <type>"
        return None, str(error)
    except RecursionError:  # cyclic, or nested past Python's limit
        return None, "output not JSON: nested too deep"
    if not isinstance(output, dict):
        return None, f"output not a dict: {type(output).__name__}"
    return cast("dict[str, Any]", output), None


def _line(agent: Callable[[dict[str, Any]], Any], case: dict[str, Any], redactor: Redactor):
    """One case's redacted line, checked to be storable as write_outputs will store it."""
    output, error = _call(agent, case)
    safe = strip_fingerprints(redactor.redact({"output": output, "error": error}).value)
    canonical(safe).encode("utf-8")  # a lone surrogate converts, but can't be written
    return cast("dict[str, Any]", safe)


def run_suite(
    suite: Suite, *, redactor: Redactor, include_unreviewed: bool, now: datetime
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Calls the agent once per case not skipped; returns the header and a redacted line per
    case. A secret in an output becomes `[REDACTED:kind]`, with no fingerprint."""
    if not suite.agent:
        raise CaseError("no agent: set agent in suite.yaml or pass --agent")
    with contextlib.redirect_stdout(sys.stderr):  # a print at import mustn't reach stdout
        agent = import_agent(suite.agent)
    header = {
        "agent": suite.agent,
        "agent_version": agent_version(suite.agent),
        "git_sha": git_sha(suite.folder),
        "model": suite.model,
        "ran_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    lines: dict[str, dict[str, Any]] = {}
    for case in suite.cases:
        if (case.get("unreviewed") is True and not include_unreviewed) or not case["checks"]:
            continue
        # the one place every case passes: whatever goes wrong in it, short of Ctrl-C, costs
        # that case an error line, never the run (and what the agent prints goes to stderr)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                safe = _line(agent, case, redactor)
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            safe = {"output": None, "error": f"output not storable: {type(error).__name__}"}
        lines[case["id"]] = {"case": case["id"], "input_hash": input_hash(case)} | safe
    return header, lines


# --- files ----------------------------------------------------------------------------


def _replace(path: Path, text: str) -> None:
    """Written whole beside the file, then swapped in: an interrupted write leaves the old."""
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with partial.open("w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def read_outputs(folder: Path) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    path = folder / OUTPUTS
    if not path.exists():
        return None, {}
    try:
        rows: list[Any] = [json.loads(r) for r in path.read_text(encoding="utf-8").splitlines()]
        for row in rows:  # what grade prints must be writable text: no lone surrogates
            canonical(row).encode("utf-8")
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise CaseError(DAMAGED) from error
    dicts = [cast("dict[str, Any]", r) if isinstance(r, dict) else {} for r in rows]
    if not dicts or not isinstance(dicts[0].get("run"), dict):
        raise CaseError(DAMAGED)
    lines: dict[str, dict[str, Any]] = {}
    for line in dicts[1:]:
        case = line.get("case")
        if (
            not isinstance(case, str)
            or case in lines
            or not isinstance(line.get("input_hash"), str)
            or not isinstance(line.get("output"), dict | None)
            or not isinstance(line.get("error"), str | None)
        ):
            raise CaseError(DAMAGED)
        lines[case] = line
    return cast("dict[str, Any]", dicts[0]["run"]), lines


def write_outputs(folder: Path, header: dict[str, Any], lines: dict[str, dict[str, Any]]) -> None:
    rows = [canonical({"run": header}), *(canonical(lines[k]) for k in sorted(lines))]
    _replace(folder / OUTPUTS, "".join(f"{row}\n" for row in rows))


def read_baseline(folder: Path) -> dict[str, dict[str, str]] | None:
    path = folder / BASELINE
    if not path.exists():
        return None
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise CaseError(f"{BASELINE} is damaged: {error}") from error
    if not isinstance(baseline, dict) or not all(
        isinstance(v, dict) and all(isinstance(s, str) for s in cast("dict[str, Any]", v).values())
        for v in cast("dict[str, Any]", baseline).values()
    ):
        raise CaseError(f"{BASELINE} is damaged: expected case -> check key -> state")
    return cast("dict[str, dict[str, str]]", baseline)


def write_baseline(folder: Path, baseline: dict[str, dict[str, str]]) -> None:
    _replace(folder / BASELINE, json.dumps(baseline, sort_keys=True, indent=2) + "\n")
