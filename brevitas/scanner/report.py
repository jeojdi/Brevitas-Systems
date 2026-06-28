"""Human-readable rendering of scan results and codemod diffs."""
from __future__ import annotations

import os

from .models import Recommendation, ScanReport

try:
    from rich.console import Console
    from rich.syntax import Syntax
    from rich.table import Table
    _RICH = True
except ImportError:  # pragma: no cover - rich is a hard dependency, fallback for safety
    _RICH = False


_REC_STYLE = {
    Recommendation.APPLY: ("[green]wrap[/green]", "wrap"),
    Recommendation.MANUAL: ("[yellow]manual[/yellow]", "manual"),
    Recommendation.DONE: ("[dim]done[/dim]", "done"),
}


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:
        return path


def render_report(report: ScanReport, console=None) -> None:
    """Print a summary of detected clients and call sites."""
    if not _RICH:
        _render_plain(report)
        return
    console = console or Console()
    clients = report.clients

    if not clients and not report.call_sites:
        console.print(
            f"\n[bold]Scanned {report.files_scanned} file(s).[/bold] "
            "No LLM API calls found — nothing for Brevitas to sit in front of.\n"
        )
        _render_errors(report, console)
        return

    table = Table(title="LLM client constructions", title_style="bold", show_lines=False)
    table.add_column("Location", style="cyan", no_wrap=True)
    table.add_column("Provider")
    table.add_column("Client")
    table.add_column("Action")
    table.add_column("Why", style="dim")
    for f in sorted(clients, key=lambda x: (x.path, x.line)):
        marker = _REC_STYLE[f.recommendation][0]
        table.add_row(_rel(f.location), f.provider, f.symbol, marker, f.reason)
    console.print()
    console.print(table)

    applicable = report.applicable
    manual = [f for f in clients if f.recommendation is Recommendation.MANUAL]
    done = [f for f in clients if f.recommendation is Recommendation.DONE]

    console.print(
        f"\n[bold]{report.files_scanned}[/bold] files · "
        f"[bold]{len(clients)}[/bold] clients · "
        f"[bold]{len(report.call_sites)}[/bold] call sites"
    )
    if report.is_pipeline:
        console.print(
            "[green]Multi-agent pipeline detected[/green] — Brevitas compresses "
            "context between every hop."
        )
    console.print(
        f"  [green]{len(applicable)}[/green] ready to wrap · "
        f"[yellow]{len(manual)}[/yellow] need manual review · "
        f"[dim]{len(done)} already wrapped[/dim]"
    )
    if applicable:
        console.print(
            "\nNext: [yellow]brevitas apply[/yellow] to preview the changes, "
            "then [yellow]brevitas apply --write[/yellow] to apply them."
        )
        console.print("[dim]Set BREVITAS_API_KEY in your environment so wrapped calls authenticate.[/dim]")
    _render_errors(report, console)
    console.print()


def _render_errors(report: ScanReport, console) -> None:
    if report.errors:
        console.print(f"\n[yellow]{len(report.errors)} file(s) skipped:[/yellow]")
        for path, msg in report.errors[:10]:
            console.print(f"  [dim]{_rel(path)}: {msg}[/dim]")


def render_diff(changes, console=None) -> None:
    """Print unified diffs for a list of FileChange objects."""
    if not _RICH:
        for change in changes:
            print(change.diff)
        return
    console = console or Console()
    for change in changes:
        console.print(f"\n[bold cyan]{_rel(change.path)}[/bold cyan] "
                      f"[dim]({change.wrapped} client(s) wrapped)[/dim]")
        console.print(Syntax(change.diff, "diff", theme="ansi_dark", background_color="default"))


def _render_plain(report: ScanReport) -> None:
    print(f"Scanned {report.files_scanned} file(s).")
    for f in report.clients:
        tag = _REC_STYLE[f.recommendation][1]
        print(f"  [{tag}] {_rel(f.location)}  {f.symbol}  ({f.reason})")
    print(f"{len(report.clients)} clients, {len(report.call_sites)} call sites, "
          f"{len(report.applicable)} ready to wrap.")
