"""
brevitas CLI
"""
from __future__ import annotations

import os
import sys

import click

try:
    from rich.console import Console
    from rich.table import Table
    _console = Console()
except ImportError:
    _console = None


def _print(msg: str) -> None:
    if _console:
        _console.print(msg)
    else:
        print(msg)


@click.group()
def main() -> None:
    """Brevitas — drop compression between your agents."""


@main.command()
@click.option("--port",     default=4242,                    show_default=True, help="Proxy listen port")
@click.option("--api-key",  default="",  envvar="BREVITAS_API_KEY",            help="Your Brevitas API key")
@click.option("--base-url", default="http://localhost:8000", envvar="BREVITAS_BASE_URL", show_default=True, help="Brevitas API base URL")
@click.option("--host",     default="127.0.0.1",             show_default=True, help="Bind host")
def start(port: int, api_key: str, base_url: str, host: str) -> None:
    """Start the local Brevitas proxy server."""
    if api_key:
        os.environ["BREVITAS_API_KEY"]  = api_key
    if base_url:
        os.environ["BREVITAS_BASE_URL"] = base_url

    from . import configure
    configure(api_key=api_key or os.getenv("BREVITAS_API_KEY", ""), base_url=base_url)

    _print(f"\n[bold green]Brevitas proxy starting on {host}:{port}[/bold green]")
    _print(f"  Compression API → [cyan]{base_url}[/cyan]")
    _print("\n[dim]Set your SDK base URL:[/dim]")
    _print(f"  [yellow]ANTHROPIC_BASE_URL=http://{host}:{port}[/yellow]")
    _print(f"  [yellow]OPENAI_BASE_URL=http://{host}:{port}/openai[/yellow]\n")

    try:
        import uvicorn
        from .proxy import proxy_app
        uvicorn.run(proxy_app, host=host, port=port, log_level="warning")
    except ImportError:
        _print("[red]uvicorn not installed. Run: pip install brevitas-systems[/red]")
        sys.exit(1)


@main.command()
@click.argument("path", default=".", required=False)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON instead of a table.")
def scan(path: str, as_json: bool) -> None:
    """Scan a codebase for LLM API calls Brevitas can sit in front of."""
    from .scanner import scan_path
    from .scanner.report import render_report

    report = scan_path(path)

    if as_json:
        import json as _json
        from dataclasses import asdict
        click.echo(_json.dumps({
            "files_scanned": report.files_scanned,
            "is_pipeline": report.is_pipeline,
            "findings": [
                {**asdict(f), "kind": f.kind.value, "recommendation": f.recommendation.value}
                for f in report.findings
            ],
            "errors": report.errors,
        }, indent=2))
        return

    render_report(report)


@main.command()
@click.argument("path", default=".", required=False)
@click.option("--write", "-w", is_flag=True, help="Apply the changes (default: dry-run diff).")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt when writing.")
def apply(path: str, write: bool, yes: bool) -> None:
    """Wrap detected LLM clients with brevitas.wrap() (dry-run unless --write)."""
    from .scanner import plan_changes, scan_path, write_changes
    from .scanner.report import render_diff

    report = scan_path(path)
    changes = plan_changes(report)

    if not changes:
        _print("[dim]No applicable clients to wrap. Run [yellow]brevitas scan[/yellow] for details.[/dim]")
        return

    render_diff(changes)
    total = sum(c.wrapped for c in changes)
    _print(f"\n[bold]{total}[/bold] client(s) across [bold]{len(changes)}[/bold] file(s).")

    if not write:
        _print("[dim]Dry run. Re-run with [yellow]--write[/yellow] to apply these changes.[/dim]")
        return

    if not yes and not click.confirm("Apply these changes?", default=False):
        _print("[dim]Aborted.[/dim]")
        return

    written = write_changes(changes)
    _print(f"[green]✓ Wrapped {total} client(s) in {written} file(s).[/green]")
    _print("[dim]Set BREVITAS_API_KEY so the wrapped calls authenticate.[/dim]")


@main.command()
@click.argument("key")
@click.argument("value")
def config(key: str, value: str) -> None:
    """Set a config value (api-key, base-url)."""
    cfg_map = {"api-key": "BREVITAS_API_KEY", "base-url": "BREVITAS_BASE_URL"}
    env_key = cfg_map.get(key.lower())
    if not env_key:
        _print(f"[red]Unknown config key '{key}'. Valid: {list(cfg_map)}[/red]")
        sys.exit(1)
    _print(f"[green]Set {env_key}={value}[/green]")
    _print(f"[dim]Add to your shell profile: export {env_key}={value}[/dim]")


@main.command()
@click.option("--api-key",  default="", envvar="BREVITAS_API_KEY")
@click.option("--base-url", default="http://localhost:8000", envvar="BREVITAS_BASE_URL")
def status(api_key: str, base_url: str) -> None:
    """Check connectivity to the Brevitas API."""
    import httpx
    _print(f"\nChecking [cyan]{base_url}/v1/health[/cyan] …")
    try:
        r = httpx.get(f"{base_url}/v1/health", timeout=5)
        if r.status_code == 200:
            _print("[green]✓ Brevitas API reachable[/green]")
        else:
            _print(f"[yellow]API returned {r.status_code}[/yellow]")
    except Exception as e:
        _print(f"[red]✗ Could not reach API: {e}[/red]")
        return

    if api_key:
        try:
            r = httpx.get(f"{base_url}/v1/stats", headers={"X-API-Key": api_key}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                _print(f"[green]✓ API key valid[/green]")
                _print(f"  Total calls:       {data.get('total_calls', 0)}")
                _print(f"  Total tokens saved: {data.get('total_tokens_saved', 0):,}")
                _print(f"  Total cost saved:  ${data.get('total_cost_saved_usd', 0):.4f}")
                _print(f"  Brevitas fee owed: ${data.get('total_brevitas_fee_usd', 0):.4f}")
            else:
                _print(f"[red]✗ API key invalid (status {r.status_code})[/red]")
        except Exception as e:
            _print(f"[red]✗ Stats check failed: {e}[/red]")
    else:
        _print("[dim]No API key set — set BREVITAS_API_KEY to check usage[/dim]")
