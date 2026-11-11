"""Agent Flight Recorder: record, replay and analyse AI agent runs."""

from typing import Any


def __getattr__(name: str) -> Any:
    # Looked up on first use: importlib.metadata costs ~60 ms, and the Claude Code hook
    # starts a fresh process for every event without needing the version.
    if name == "__version__":
        from importlib.metadata import version

        return version("agent-flight-recorder")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
