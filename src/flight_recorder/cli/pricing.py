"""`afr pricing`: model prices in pricing.yaml."""

from pathlib import Path
from typing import Annotated

import typer

pricing_app = typer.Typer(help="Model prices in pricing.yaml.", no_args_is_help=True)


@pricing_app.command("check")
def pricing_check(
    write: Annotated[
        bool, typer.Option("--write", help="Update pricing.yaml and its version to match.")
    ] = False,
    file: Annotated[
        Path | None, typer.Option(help="pricing.yaml to check [default: the package's own]")
    ] = None,
    page: Annotated[
        Path | None, typer.Option(help="A saved copy of the page's Markdown, instead of fetching")
    ] = None,
) -> None:
    """Compare Claude rates with Anthropic's pricing page. Exits 1 if they differ (unless
    --write), 2 if the page can't be read or no longer parses. A model or tier only one
    side has is listed as `skipped:` and not compared."""
    from flight_recorder import pricing_check as pc

    path = file or Path(pc.__file__).with_name("pricing.yaml")
    text = path.read_bytes().decode("utf-8")  # bytes: keep the file's line endings as they are
    source = page or pc.MARKDOWN_URL
    try:
        markdown = page.read_text(encoding="utf-8") if page else pc.fetch_page()
        report = pc.check(text, markdown)
    except (OSError, pc.PageFormatError) as error:
        typer.echo(f"error: {source}: {error}", err=True)
        raise typer.Exit(code=2) from error

    typer.echo(f"source: {source}")
    for skipped in report.skipped:
        typer.echo(f"skipped: {skipped}")
    if not report.changes:
        typer.echo(f"{path.name}: Claude rates match the page")
        return
    typer.echo(f"{len(report.changes)} rate(s) differ from the page:")
    for change in report.changes:
        typer.echo(f"  {change}")
    if not write:
        raise typer.Exit(code=1)
    version = pc.today()
    try:
        updated = pc.apply(text, report.changes, version)
    except ValueError as error:  # pricing.yaml lacks a version or the mapping to add a rate to
        typer.echo(f"error: {path}: {error}", err=True)
        raise typer.Exit(code=2) from error
    path.write_bytes(updated.encode("utf-8"))
    typer.echo(f"updated {path}, version {version}")
