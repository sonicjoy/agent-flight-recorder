"""Keyword search over issues (BM25). Deliberately simple: no stemming, no synonyms.

Embeddings replace this in #11; until then, reworded duplicates are expected misses.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

from issue_triager.fixtures import TriageCase

TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "this",
        "to",
        "we",
        "with",
        "when",
        "what",
        "which",
        "will",
        "would",
        "should",
        "can",
        "could",
        "do",
        "does",
        "not",
        "no",
        "our",
        "us",
        "you",
        "your",
    ]
)


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


class KeywordIndex:
    """BM25 over title + body. The title counts twice: it's the densest signal."""

    def __init__(self, cases: Sequence[TriageCase], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.cases = list(cases)
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(f"{c.title} {c.title} {c.body}")) for c in self.cases]
        lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(lengths) / len(lengths) if lengths else 0.0
        self.lengths = lengths
        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(doc.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(
        self, query: str, *, exclude: str | None = None, limit: int = 5
    ) -> list[tuple[TriageCase, float]]:
        terms = tokenize(query)
        scored: list[tuple[TriageCase, float]] = []
        for case, doc, length in zip(self.cases, self.docs, self.lengths, strict=True):
            if case.id == exclude:
                continue
            score = 0.0
            for term in terms:
                tf = doc.get(term, 0)
                if tf:
                    norm = self.k1 * (1 - self.b + self.b * length / self.avg_len)
                    score += self.idf[term] * tf * (self.k1 + 1) / (tf + norm)
            if score > 0:
                scored.append((case, score))
        scored.sort(key=lambda pair: (-pair[1], int(pair[0].id)))
        return scored[:limit]
