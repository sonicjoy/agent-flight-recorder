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
