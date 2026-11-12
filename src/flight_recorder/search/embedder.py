"""Text to vectors, locally: bge-small through fastembed (ONNX), no API key.

The model is Qdrant's quantised ONNX conversion of BAAI/bge-small-en-v1.5 (about 67 MB). It
downloads once from Hugging Face into `AFR_HOME/models`, pinned to one revision, so a changed
upstream repo can't swap it. Nothing else leaves the machine: texts are embedded on the CPU.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from fastembed import TextEmbedding  # pyright: ignore[reportMissingTypeStubs]

MODEL = "BAAI/bge-small-en-v1.5"
MODEL_REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
MODEL_REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
MODEL_FILES = [
    "config.json",
    "model_optimized.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
]
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
"""bge's instruction for a short query that should find longer passages."""
BATCH = 32


class Embedder(Protocol):
    """Turns text into unit-length vectors; `model` is stored with each vector."""

    model: str

    def documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def query(self, value: str) -> list[float]: ...


def model_dir(cache: Path) -> Path:
    """The pinned model's files: from the cache without touching the network, else
    downloaded at MODEL_REVISION (showing progress, the first time only)."""
    from huggingface_hub import snapshot_download  # pyright: ignore[reportUnknownVariableType]
    from huggingface_hub.errors import LocalEntryNotFoundError

    def fetch(*, local_only: bool) -> Path:
        return Path(
            snapshot_download(
                repo_id=MODEL_REPO,
                revision=MODEL_REVISION,
                cache_dir=str(cache),
                allow_patterns=MODEL_FILES,
                local_files_only=local_only,
            )
        )

    try:
        return fetch(local_only=True)
    except LocalEntryNotFoundError:
        return fetch(local_only=False)


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
            self._loaded = TextEmbedding(
                MODEL, cache_dir=str(cache), specific_model_path=str(model_dir(cache))
            )
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
