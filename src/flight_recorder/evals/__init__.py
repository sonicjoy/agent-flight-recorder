"""Eval suites: cases written from recorded runs (#59), and the harness that runs them (#15).

An agent the harness may call is a function marked with `@agent`. `suite.yaml` is data, and
it names the function `afr eval run` imports, so only functions written as agents are
accepted: a YAML file can't pick `subprocess:run`, or afr's own `store.db:downgrade`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

MARK = "__afr_agent__"


def agent[F: Callable[..., Any]](function: F) -> F:
    """Marks `function(input: dict) -> dict` as an agent `afr eval run` may call."""
    setattr(function, MARK, True)
    return function
