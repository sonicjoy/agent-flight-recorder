from pathlib import Path

import pytest
from typer.testing import CliRunner

from flight_recorder import __version__
from flight_recorder.cli import app

runner = CliRunner()


def test_version_prints_installed_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])

    assert "Usage" in result.output
    assert "version" in result.output


def test_llm_ping_without_key_explains_what_is_missing(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.chdir(tmp_path)  # no .env here

    result = runner.invoke(app, ["llm-ping", "--model", "google_genai:gemini-3.5-flash-lite"])

    assert result.exit_code == 2
    assert "GOOGLE_API_KEY" in result.output


def test_llm_ping_rejects_bad_spec(clean_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["llm-ping", "--model", "not-a-spec"])

    assert result.exit_code == 2
    assert "<provider>:<model>" in result.output
