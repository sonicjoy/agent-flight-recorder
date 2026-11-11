"""The pricing check, offline: a saved copy of Anthropic's pricing page (11 Nov 2026)."""

import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from flight_recorder import pricing_check as pc
from flight_recorder.cli import app

PAGE = (Path(__file__).parent / "data/anthropic-pricing-2026-11-11.md").read_text(encoding="utf-8")
PRICING = (Path(pc.__file__).with_name("pricing.yaml")).read_bytes().decode("utf-8")

runner = CliRunner()


def edit_row(page: str, model: str, column: int, value: str) -> str:
    """The page with one cell of a model's row in "Model pricing" changed (column 1 = input)."""
    pattern = re.compile(rf"^\| {re.escape(model)} +\|.*$", re.MULTILINE)
    row = pattern.search(page)
    assert row is not None
    cells = row.group(0).split("|")
    cells[column + 1] = f" {value} "
    return page[: row.start()] + "|".join(cells) + page[row.end() :]


def test_page_parses_into_pricing_yaml_shape() -> None:
    page = pc.parse_page(PAGE)

    assert page["anthropic:claude-opus-5-5"] == {
        "input": 4.0,
        "cache_write_5m": 5.0,
        "cache_write_1h": 8.0,
        "cache_read": 0.2,
        "output": 20.0,
        # fast: listed as $8 / $40; cache rates from Opus 5.5's own multipliers
        "fast": {
            "input": 8.0,
            "output": 40.0,
            "cache_read": 0.4,
            "cache_write_5m": 10.0,
            "cache_write_1h": 16.0,
        },
    }
    haiku = page["anthropic:claude-haiku-5-5"]
    assert (haiku["input"], haiku["cache_read"]) == (0.10, 0.01)
    assert haiku["long_context"] == {
        "above_input_tokens": 100_000,
        "input": 0.5,
        "cache_write_5m": 0.625,
        "cache_write_1h": 1.0,
        "cache_read": 0.05,
        "output": 2.5,
    }


def test_page_names_become_specs() -> None:
    page = pc.parse_page(PAGE)

    assert "anthropic:claude-mythos-5-2" in page  # "(limited availability)" link dropped
    assert page["anthropic:claude-opus-4-8"]["fast"]["input"] == 10.0  # "Opus 5 / Opus 4.8"
    assert page["anthropic:claude-opus-5"]["fast"]["input"] == 10.0


def test_committed_pricing_matches_the_saved_page() -> None:
    assert pc.check(PRICING, PAGE) == pc.Report(changes=[], skipped=[])


def test_a_changed_rate_is_reported() -> None:
    page = edit_row(PAGE, "Claude Sonnet 5.5", 5, "$12 / MTok")

    report = pc.check(PRICING, page)

    assert [str(c) for c in report.changes] == [
        "anthropic:claude-sonnet-5-5.output: 10.00 -> 12.00"
    ]


def test_long_context_threshold_and_fast_rates_are_compared() -> None:
    page = PAGE.replace("(for prompts up to 100,000 tokens)", "(for prompts up to 200,000 tokens)")
    page = page.replace("(for prompts over 100,000 tokens)", "(for prompts over 200,000 tokens)")
    page = page.replace("| Claude Opus 5.5                 | $8 USD", "| Claude Opus 5.5 | $9 USD")

    paths = {c.path for c in pc.check(PRICING, page).changes}

    assert ("anthropic:claude-haiku-5-5", "long_context", "above_input_tokens") in paths
    assert ("anthropic:claude-opus-5-5", "fast", "input") in paths
    assert ("anthropic:claude-opus-5-5", "fast", "cache_read") in paths  # derived from input


def test_write_changes_values_and_version_and_keeps_comments() -> None:
    page = edit_row(PAGE, "Claude Haiku 5.5 (for prompts over 100,000 tokens)", 4, "$0.06 / MTok")
    report = pc.check(PRICING, page)

    updated = pc.apply(PRICING, report.changes, "2026-12-01")

    table = yaml.safe_load(updated)
    assert table["version"] == "2026-12-01"
    assert table["models"]["anthropic:claude-haiku-5-5"]["long_context"]["cache_read"] == 0.06
    assert table["models"]["anthropic:claude-haiku-5-5"]["cache_read"] == 0.01  # other tier kept
    assert pc.check(updated, page).changes == []
    assert updated.count("#") == PRICING.count("#")
    assert len(updated.splitlines()) == len(PRICING.splitlines())


def test_write_adds_a_field_the_file_lacks() -> None:
    without = PRICING.replace("    cache_read: 0.10\n", "", 1)  # Sonnet 5.5's
    report = pc.check(without, PAGE)
    assert [str(c) for c in report.changes] == [
        "anthropic:claude-sonnet-5-5.cache_read: (missing) -> 0.10"
    ]

    updated = pc.apply(without, report.changes, "2026-12-01")

    assert yaml.safe_load(updated)["models"]["anthropic:claude-sonnet-5-5"]["cache_read"] == 0.1
    assert pc.check(updated, PAGE).changes == []


def test_write_keeps_crlf_line_endings() -> None:
    crlf = PRICING.replace("\n", "\r\n")
    changes = [pc.Change(("anthropic:claude-opus-5-5", "input"), 4.0, 4.5)]

    updated = pc.apply(crlf, changes, "2026-12-01")

    assert "\r\n" in updated and "\n" not in updated.replace("\r\n", "")
    assert "    input: 4.50\r\n" in updated


def test_tiers_and_models_the_other_side_lacks_are_skipped() -> None:
    text = PRICING.replace("anthropic:claude-fable-5-1:", "anthropic:claude-fable-4-9:")
    page = pc.parse_page(PAGE)
    del page["anthropic:claude-opus-5-5"]["fast"]
    page["anthropic:claude-sonnet-5-5"]["fast"] = {"input": 4.0, "output": 20.0}

    report = pc.compare(yaml.safe_load(text), page)

    assert report.changes == []
    assert report.skipped == [
        "anthropic:claude-opus-5-5 fast: not on the pricing page",
        "anthropic:claude-sonnet-5-5 fast: on the page, not in pricing.yaml",
        "anthropic:claude-fable-4-9: not on the pricing page",
    ]


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ("# Pricing\n\nNothing here.\n", "no 'model pricing' section"),
        (PAGE.replace("Base input tokens", "Input"), "no table with columns"),
        (PAGE.replace("| $4 / MTok             |", "| contact sales |", 1), "not a price"),
    ],
)
def test_a_page_that_no_longer_parses_is_an_error(page: str, message: str) -> None:
    with pytest.raises(pc.PageFormatError, match=message):
        pc.parse_page(page)


@pytest.mark.parametrize(
    ("value", "text"), [(4.0, "4.00"), (0.125, "0.125"), (0.2, "0.20"), (12.5, "12.50"), (7, "7")]
)
def test_prices_are_written_with_two_decimals_at_least(value: float, text: str) -> None:
    assert pc._fmt(value) == text  # pyright: ignore[reportPrivateUsage]


def test_cli_reports_differences_and_writes_them(tmp_path: Path) -> None:
    pricing = tmp_path / "pricing.yaml"
    pricing.write_bytes(PRICING.encode())
    page = tmp_path / "page.md"
    page.write_text(edit_row(PAGE, "Claude Opus 5.5", 1, "$4.50 / MTok"), encoding="utf-8")
    args = ["pricing", "check", "--file", str(pricing), "--page", str(page)]

    checked = runner.invoke(app, args)
    assert checked.exit_code == 1
    assert "anthropic:claude-opus-5-5.input: 4.00 -> 4.50" in checked.output
    assert pricing.read_bytes().decode() == PRICING  # not written without --write

    written = runner.invoke(app, [*args, "--write"])
    assert written.exit_code == 0, written.output
    assert f"version {pc.today()}" in written.output
    assert runner.invoke(app, args).exit_code == 0  # now matches


def test_cli_explains_a_page_it_cannot_read(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("# Pricing\n", encoding="utf-8")

    result = runner.invoke(app, ["pricing", "check", "--page", str(page)])

    assert result.exit_code == 2
    assert "no 'model pricing' section" in result.output


def test_a_row_with_missing_cells_is_a_format_error_not_a_crash() -> None:
    page = PAGE.replace("| $10 / MTok             |\n| Claude Sonnet 5 ", "\n| Claude Sonnet 5 ", 1)

    with pytest.raises(pc.PageFormatError, match="cells"):
        pc.parse_page(page)


def test_an_escaped_pipe_in_a_name_is_not_a_column() -> None:
    page = PAGE.replace("| Claude Opus 4.7  ", r"| Claude Opus 4.7 \| preview ", 1)

    assert pc.check(PRICING, page).changes == []


def test_a_null_rate_counts_as_missing() -> None:
    text = PRICING.replace("    cache_read: 0.10\n", "    cache_read: null\n", 1)  # Sonnet 5.5

    assert [str(c) for c in pc.check(text, PAGE).changes] == [
        "anthropic:claude-sonnet-5-5.cache_read: (missing) -> 0.10"
    ]


def test_cli_write_without_a_version_is_an_error(tmp_path: Path) -> None:
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text(re.sub(r"^version: .*\n", "", PRICING, flags=re.MULTILINE), "utf-8")
    page = tmp_path / "page.md"
    page.write_text(edit_row(PAGE, "Claude Opus 5.5", 1, "$4.50 / MTok"), encoding="utf-8")

    result = runner.invoke(
        app, ["pricing", "check", "--file", str(pricing), "--page", str(page), "--write"]
    )

    assert result.exit_code == 2
    assert "no top-level version" in result.output


def test_a_page_with_none_of_our_models_is_a_format_error_not_a_pass() -> None:
    """Every model skipped means the names changed: someone must look, not a green run."""
    renamed = {f"{spec}-renamed": rates for spec, rates in pc.parse_page(PAGE).items()}

    with pytest.raises(pc.PageFormatError, match=r"none of pricing.yaml's Claude models"):
        pc.compare(yaml.safe_load(PRICING), renamed)
