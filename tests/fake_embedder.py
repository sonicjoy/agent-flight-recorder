"""A deterministic, offline embedder for tests (search, and the MCP server's search_runs)."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Deterministic and offline: each word adds to one dimension, so texts that share
    words are close. Enough to test the plumbing, not real meaning (the smoke test does)."""

    model = "fake-bag-of-words"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _vector(self, value: str) -> list[float]:
        vector = [0.0] * 384
        for word in WORD.findall(value.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 384] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    def query(self, value: str) -> list[float]:
        return self._vector(value)
