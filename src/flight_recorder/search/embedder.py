"""Text to vectors, locally: bge-small through fastembed (ONNX), no API key.

The model (about 67 MB) downloads once from Hugging Face into `AFR_HOME/models`. Nothing
else leaves the machine: texts are embedded on the CPU.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from flight_recorder.store.models import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from fastembed import TextEmbedding  # pyright: ignore[reportMissingTypeStubs]

__all__ = ["EMBEDDING_DIMENSIONS", "MODEL", "Embedder", "FastEmbedder", "default_embedder"]

MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
"""bge's instruction for a short query that should find longer passages."""
BATCH = 32


class Embedder(Protocol):
    """Turns text into unit-length vectors; `model` is stored with each vector."""

    model: str

    def documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def query(self, value: str) -> list[float]: ...


class FastEmbedder:
    """bge-small-en-v1.5, loaded on first use."""

    model = MODEL

    def __init__(self) -> None:
        self._loaded: TextEmbedding | None = None

    def _engine(self) -> TextEmbedding:
        if self._loaded is None:
            from fastembed import TextEmbedding  # pyright: ignore[reportMissingTypeStubs]

            from flight_recorder.settings import Settings

            cache = Settings().afr_home / "models"
            self._loaded = TextEmbedding(MODEL, cache_dir=str(cache))
        return self._loaded

    def documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._engine().embed(list(texts), batch_size=BATCH)
        return [[float(x) for x in vector] for vector in vectors]

    def query(self, value: str) -> list[float]:
        (vector,) = self._engine().embed([QUERY_PREFIX + value])
        return [float(x) for x in vector]


def default_embedder() -> Embedder:
    """The embedder the commands use (tests swap in a fake)."""
    return FastEmbedder()
