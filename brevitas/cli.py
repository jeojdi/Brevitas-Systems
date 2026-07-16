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


@main.command()
@click.argument("prompt", required=False)
@click.option("--file", "-f", "path", default="", help="Read the prompt from a file.")
@click.option("--task", "-t", default="", help="Task hint: creative|code|summarize|reasoning|extraction (else auto-detected).")
@click.option("--rate", "-r", type=float, default=None, help="Force a fixed keep-rate (0.1-1.0). Default: smart per-task.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a summary.")
def optimize(prompt: str, path: str, task: str, rate, as_json: bool) -> None:
    """Shrink a single prompt's tokens (smart, task-aware). Reads PROMPT, --file, or stdin.

    Examples:
        brevitas optimize "Make me a marketing reel for our oak table"
        cat prompt.txt | brevitas optimize
        brevitas optimize -f prompt.txt --task code
    """
    import json as _json
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    elif prompt:
        text = prompt
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        _print("[red]No prompt given.[/red] Pass it as an argument, with -f FILE, or pipe via stdin.")
        sys.exit(1)

    if rate is not None:
        from token_efficiency_model.lossless.prompt_optimizer import optimize_prompt as _opt
        r = _opt(text, rate=rate)
        task_name, used_rate = None, rate
        optimized, tb, ta, sp, method, lossy, note = (
            r.optimized, r.tokens_before, r.tokens_after, r.saved_pct, r.method, r.lossy, r.note)
    else:
        from token_efficiency_model.lossless.task_router import TaskCompressionRouter
        res = TaskCompressionRouter().route(text, task_hint=task or None)
        o = res.optimization
        task_name, used_rate = res.task, res.rate
        optimized, tb, ta, sp, method, lossy, note = (
            o.optimized, o.tokens_before, o.tokens_after, o.saved_pct, o.method, o.lossy, o.note)

    if as_json:
        click.echo(_json.dumps({
            "task": task_name, "rate": used_rate, "tokens_before": tb, "tokens_after": ta,
            "saved_pct": sp, "method": method, "lossy": lossy, "note": note,
            "optimized_prompt": optimized,
        }, indent=2))
        return

    _print(f"\n[bold]Task:[/bold] {task_name or 'fixed-rate'}   [bold]rate:[/bold] {used_rate}")
    _print(f"[bold]Tokens:[/bold] {tb} -> {ta}   [green]{sp}% saved[/green]   "
           f"[dim]({method}{', lossy' if lossy else ', lossless'})[/dim]")
    if note:
        _print(f"[dim]{note}[/dim]")
    _print("\n[bold]Optimized prompt:[/bold]\n" + optimized)


@main.command()
@click.argument("path", default=".", required=False)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def analyze(path: str, as_json: bool) -> None:
    """Scan ANY codebase for LLM API calls (SDK + raw HTTP) and recommend a per-call
    strategy: optimize (compress simple/creative prompts) vs lossless (keep complex ones,
    save via caching)."""
    from .scanner.broad import analyze_path
    rep = analyze_path(path)

    if as_json:
        import json as _json
        click.echo(_json.dumps({
            "files_scanned": rep.files_scanned,
            "optimize": len(rep.optimize), "lossless": len(rep.lossless),
            "calls": [{
                "location": c.location, "provider": c.provider, "transport": c.transport,
                "call_site_id": c.call_site_id,
                "task": c.task, "complexity": c.complexity, "strategy": c.strategy.value,
                "prompt_excerpt": c.prompt_excerpt, "reason": c.reason,
            } for c in rep.calls],
        }, indent=2))
        return

    if not rep.calls:
        _print(f"[dim]Scanned {rep.files_scanned} files — no LLM API calls found.[/dim]")
        return
    _print(f"\n[bold]{len(rep.calls)}[/bold] LLM API call(s) across {rep.files_scanned} files:\n")
    for c in rep.calls:
        color = "yellow" if c.strategy.value == "optimize" else ("green" if c.strategy.value == "lossless" else "dim")
        _print(f"  [cyan]{c.location}[/cyan]  {c.provider}/{c.transport}  "
               f"[{color}]{c.strategy.value.upper()}[/{color}]  [dim]{c.reason}[/dim]")
    _print(f"\n[bold]Recommend[/bold]: [yellow]{len(rep.optimize)} OPTIMIZE[/yellow] "
           f"(compress) · [green]{len(rep.lossless)} LOSSLESS[/green] (keep + cache)")


_PROVIDER_KEY_ENVS = {
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "deepseek": ["Deepseek_api_key", "DEEPSEEK_API_KEY"],
    "groq": ["GROQ_API_KEY"],
}


@main.command()
@click.argument("path", default=".", required=False)
@click.option("--apply", "do_apply", is_flag=True,
              help="Write the suggested wrap() changes (asks for confirmation).")
@click.option("--ai", "use_ai", is_flag=True,
              help="AI-assisted pass over files the static scanner can't classify "
                   "(uses YOUR local provider key; nothing is sent to Brevitas).")
def init(path: str, do_apply: bool, use_ai: bool) -> None:
    """One-command onboarding: find your LLM call sites, wire Brevitas in, start saving.

    Scans the workspace (static analysis; add --ai for tricky codebases), reports every
    call site and provider, checks which API keys are configured locally, and shows the
    two integration paths. Keys never leave your machine.
    """
    from .scanner import plan_changes, scan_path, write_changes
    from .scanner.broad import analyze_path
    from .scanner.report import render_diff, render_report

    _print(f"\n[bold]Brevitas onboarding[/bold] — scanning [cyan]{path}[/cyan] …")
    report = scan_path(path)
    broad = analyze_path(path)

    # 1) what we found
    render_report(report)
    providers = sorted({c.provider for c in broad.calls if c.provider and c.provider != "unknown"})
    raw_calls = [c for c in broad.calls if c.transport != "sdk"]
    if raw_calls:
        _print(f"\n[bold]{len(raw_calls)}[/bold] raw-HTTP LLM call(s) (proxy integration recommended):")
        for c in raw_calls[:10]:
            _print(f"  [cyan]{c.location}[/cyan]  {c.provider}  [dim]{c.reason}[/dim]")

    # 1b) optional AI fallback on unresolved files
    if use_ai:
        from pathlib import Path as _P
        known = {f.path for f in report.findings} | {c.location.split(":")[0] for c in broad.calls}
        candidates = [p for p in _P(path).rglob("*.py")
                      if str(p) not in known and p.stat().st_size > 200][:20]
        from .scanner.ai_assist import ai_classify_files
        ai_hits = ai_classify_files(candidates)
        if ai_hits:
            _print(f"\n[bold]AI-assisted pass[/bold] found {len(ai_hits)} more call site(s):")
            for h in ai_hits:
                _print(f"  [cyan]{h['file']}:{h['line']}[/cyan]  {h.get('provider','?')} "
                       f"[dim]{h.get('snippet','')}[/dim]")
        else:
            _print("\n[dim]AI-assisted pass: nothing additional found "
                   "(or no local provider key configured).[/dim]")

    # 2) local key checklist — keys stay in YOUR environment
    _print("\n[bold]API keys (read from your local env/.env — never sent to Brevitas):[/bold]")
    for prov in providers or ["openai", "anthropic", "deepseek"]:
        envs = _PROVIDER_KEY_ENVS.get(prov, [])
        found = next((e for e in envs if os.environ.get(e)), None)
        if found:
            _print(f"  [green]✓ {prov}[/green]  ({found} set)")
        elif envs:
            _print(f"  [yellow]○ {prov}[/yellow]  set {envs[0]} in your environment or .env")

    # 3) integration menu
    _print("\n[bold]Pick an integration (both are drop-in):[/bold]")
    _print("  [bold]A. Zero-code proxy[/bold] — no code changes:")
    _print("     [yellow]brevitas start[/yellow]   then in your app's environment:")
    _print("     [yellow]export ANTHROPIC_BASE_URL=http://localhost:4242[/yellow]")
    _print("     [yellow]export OPENAI_BASE_URL=http://localhost:4242/openai[/yellow]  "
           "[dim](also routes DeepSeek/Groq by model name)[/dim]")
    _print("  [bold]B. One-line wrap[/bold] — per client object:")
    _print("     [yellow]client = brevitas.wrap(openai.OpenAI())[/yellow]  "
           f"[dim](run [yellow]brevitas apply{' --write' if not do_apply else ''}[/yellow] "
           "to do this automatically)[/dim]")

    # 4) optional apply
    if do_apply:
        changes = plan_changes(report)
        if not changes:
            _print("\n[dim]No wrappable clients found for --apply.[/dim]")
            return
        _print("")
        render_diff(changes)
        if click.confirm("\nApply these changes?", default=False):
            written = write_changes(changes)
            _print(f"[green]✓ Wrapped {sum(c.wrapped for c in changes)} client(s) "
                   f"in {written} file(s).[/green]")
        else:
            _print("[dim]Skipped. Re-run with --apply when ready.[/dim]")

    _print("\n[bold green]Savings start on your very next call[/bold green] — byte-preserving "
           "caching is automatic. Context-reducing retrieval stays off unless you explicitly "
           "enable it after a paired quality test. Check [yellow]brevitas status[/yellow] "
           "for numbers.\n")


if __name__ == "__main__":
    main()
