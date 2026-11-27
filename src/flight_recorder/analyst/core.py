"""The analyst's core (docs/designs/analyst.md §3), the agent #23 evaluates: items in, groups
out. Grouping is plain code, so the same items always give the same groups; the model only
names each group (judgment.py). Every parameter is in the input, so changing one makes saved
eval outputs stale.
"""

from __future__ import annotations

from typing import Any, cast

from flight_recorder.analyst import judgment
from flight_recorder.evals import agent

VERSION = "1"
ITEM_KEYS = ("id", "run", "started", "signature", "text")


def validate_input(input: Any) -> None:
    """Raises ValueError naming the first problem with the input."""
    if not isinstance(input, dict):
        raise ValueError("the input must be an object")
    given = cast("dict[str, Any]", input)
    if given.get("version") != VERSION:
        raise ValueError(f"version must be {VERSION!r}")
    params = given.get("params")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = cast("dict[str, Any]", params)
    model = params.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("params.model must be a model spec or null")
    for key in ("max_groups", "max_shown"):
        value = params.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"params.{key} must be a whole number of at least 1")
    items = given.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    for entry in cast("list[Any]", items):
        if not isinstance(entry, dict) or any(
            not isinstance(cast("dict[str, Any]", entry).get(k), str) for k in ITEM_KEYS
        ):
            raise ValueError(f"each item needs text values for {', '.join(ITEM_KEYS)}")


def _newest(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(members, key=lambda m: (m["started"], m["id"]), reverse=True)


@agent
def analyse(input: dict[str, Any]) -> dict[str, Any]:
    validate_input(input)
    params: dict[str, Any] = input["params"]
    by_signature: dict[str, list[dict[str, Any]]] = {}
    for entry in input["items"]:
        by_signature.setdefault(entry["signature"], []).append(entry)
    ordered = sorted(by_signature.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    kept, dropped = ordered[: params["max_groups"]], ordered[params["max_groups"] :]
    groups: list[dict[str, Any]] = []
    for signature, members in kept:
        shown = _newest(members)[: params["max_shown"]]
        if params["model"] is None:
            named: dict[str, Any] = {"name": signature, "cause": None, "error": None,
                                     "representatives": [m["id"] for m in shown[:3]]}  # fmt: skip
        else:
            named = judgment.judge_group(params["model"], signature, shown)
        groups.append({"signature": signature,
                       "members": sorted(m["id"] for m in members),
                       "name": named["name"], "cause": named["cause"],
                       "representatives": named["representatives"],
                       "error": named["error"]})  # fmt: skip
    return {
        "groups": groups,
        "dropped_groups": len(dropped),
        "dropped_items": sum(len(members) for _, members in dropped),
    }
