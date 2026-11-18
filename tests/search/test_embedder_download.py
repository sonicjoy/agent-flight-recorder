"""`download=False`: a missing model is an error the MCP server can report, never a fetch (#24)."""

from __future__ import annotations

from pathlib import Path

import pytest

from flight_recorder.search.embedder import ModelNotDownloaded, model_dir


def test_a_missing_model_raises_instead_of_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # a guard: a download attempt fails, not hangs
    with pytest.raises(ModelNotDownloaded):
        model_dir(tmp_path, download=False)
    assert not any(tmp_path.rglob("*.onnx"))
