"""
brevitas CLI
"""
from __future__ import annotations

import os
import re
import sys

import click

from .config import DEFAULT_BASE_URL, DEFAULT_DASHBOARD_URL, check_base_url

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


def _mask(value: str) -> str:
    """Show enough of a secret to recognise it, never enough to use it."""
    if len(value) <= 8:
        return "…"
    return f"{value[:4]}…{value[-4:]}"


@click.group()
def main() -> None:
    """Brevitas — drop compression between your agents."""


@main.command()
@click.option("--port",     default=4242,                    show_default=True, help="Proxy listen port")
@click.option("--api-key",  default="",  envvar="BREVITAS_API_KEY",            help="Your Brevitas API key")
@click.option("--base-url", default=DEFAULT_BASE_URL, envvar="BREVITAS_BASE_URL", show_default=True, help="Brevitas API base URL (bare origin, no /v1)")
@click.option("--host",     default="127.0.0.1",             show_default=True, help="Bind host")
def start(port: int, api_key: str, base_url: str, host: str) -> None:
    """Start the local Brevitas proxy server."""
    if api_key:
        os.environ["BREVITAS_API_KEY"]  = api_key
    if base_url:
        check_base_url(base_url)
        os.environ["BREVITAS_BASE_URL"] = base_url
    # Per-request x-brevitas-source headers still win inside parse_brevitas_headers.
    os.environ.setdefault("BREVITAS_SOURCE", "cli")

    from . import configure
    configure(api_key=api_key or os.getenv("BREVITAS_API_KEY", ""), base_url=base_url)

    _print(f"\n[bold green]Brevitas proxy starting on {host}:{port}[/bold green]")
    _print(f"  Compression API → [cyan]{base_url}[/cyan]")
    _print("\n[dim]Set your SDK base URL:[/dim]")
    _print(f"  [yellow]ANTHROPIC_BASE_URL=http://{host}:{port}[/yellow]")
    _print(f"  [yellow]OPENAI_BASE_URL=http://{host}:{port}/openai[/yellow]\n")
    # Say this out loud rather than letting an invoice say it: receipts from this
    # local proxy are posted to POST /v1/usage, which the hosted API records with
    # authoritative=false by design (anti-forgery — the server never signed the
    # numbers it is being told). They are analytics, not billable usage. The
    # billable path is the hosted endpoint: run `brevitas connect`.
    _print("[dim]Local-proxy receipts are recorded authoritative=false (analytics, not "
           "billing).\n  For metered savings, use the hosted endpoint: "
           "[/dim][yellow]brevitas connect[/yellow]\n")

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
    if written < len(changes):
        _print(f"[yellow]{len(changes) - written} file(s) skipped — changed on disk since the "
               "scan. Re-run [bold]brevitas apply --write[/bold] for those.[/yellow]")
    _print("[dim]Set BREVITAS_API_KEY so the wrapped calls authenticate.[/dim]")


@main.command()
@click.argument("key")
@click.argument("value", required=False)
def config(key: str, value: str | None) -> None:
    """Show the export needed for a config value (api-key, base-url).

    Brevitas keeps no config file: the SDK reads its settings from the environment
    at import time, so this command tells you what to export — it does NOT persist
    anything. Secrets are echoed masked; pass the value on stdin/prompt rather than
    on the command line to keep it out of your shell history and the process table.
    """
    cfg_map = {"api-key": ("BREVITAS_API_KEY", True), "base-url": ("BREVITAS_BASE_URL", False)}
    entry = cfg_map.get(key.lower())
    if entry is None:
        _print(f"[red]Unknown config key '{key}'. Valid: {list(cfg_map)}[/red]")
        sys.exit(1)
    env_key, secret = entry
    if not value:
        value = click.prompt(env_key, hide_input=secret, default="", show_default=False)
    if not value:
        _print(f"[red]No value given for {env_key}.[/red]")
        sys.exit(1)
    if not secret:
        check_base_url(value)
    shown = _mask(value) if secret else value
    _print("[yellow]Nothing was written[/yellow] — Brevitas has no config file.")
    _print("[bold]Add this to your shell profile, then restart your app:[/bold]")
    _print(f"  export {env_key}={shown}"
           + ("   [dim](masked — paste the real value)[/dim]" if secret else ""))


@main.command()
@click.option("--api-key",  default="", envvar="BREVITAS_API_KEY")
@click.option("--base-url", default=DEFAULT_BASE_URL, envvar="BREVITAS_BASE_URL")
def status(api_key: str, base_url: str) -> None:
    """Check connectivity to the Brevitas API."""
    import httpx
    check_base_url(base_url)
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
                _print(f"  Provider input tokens avoided: {data.get('total_provider_input_tokens_avoided', 0):,}")
                _print(f"  Model calls avoided:           {data.get('total_calls_avoided', 0):,}")
                _print(f"  Native cache discount:         ${data.get('total_native_cache_discount_usd', 0):.4f}")
                lift = data.get('total_brevitas_incremental_savings_usd')
                _print(f"  Brevitas lift vs control:      {'not measured' if lift is None else f'${lift:.4f}'}")
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
@click.option("--include-excerpts", is_flag=True,
              help="Include the nearby prompt text in --json output. Off by default: excerpts "
                   "are lifted from your source, so reports are safe to share without them.")
def analyze(path: str, as_json: bool, include_excerpts: bool) -> None:
    """Scan ANY codebase for LLM API calls (SDK + raw HTTP) and recommend a per-call
    strategy: optimize (compress simple/creative prompts) vs lossless (keep complex ones,
    save via caching)."""
    from .scanner.broad import analyze_path
    rep = analyze_path(path)

    if as_json:
        import json as _json

        root = path if os.path.isdir(path) else (os.path.dirname(path) or ".")

        def _rel(call) -> str:
            # These reports get attached to threads and CI logs — no absolute paths.
            try:
                return f"{os.path.relpath(call.path, start=root)}:{call.line}"
            except ValueError:                      # different drive on Windows
                return f"{os.path.basename(call.path)}:{call.line}"

        click.echo(_json.dumps({
            "files_scanned": rep.files_scanned,
            "optimize": len(rep.optimize), "lossless": len(rep.lossless),
            "calls": [{
                "location": _rel(c), "provider": c.provider, "transport": c.transport,
                "call_site_id": c.call_site_id,
                "task": c.task, "complexity": c.complexity, "strategy": c.strategy.value,
                **({"prompt_excerpt": c.prompt_excerpt} if include_excerpts else {}),
                "reason": c.reason,
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


@main.command()
@click.argument("path", default=".", required=False)
@click.option("--format", "fmt", type=click.Choice(["json", "markdown"]), default="markdown",
              show_default=True, help="Output format.")
def audit(path: str, fmt: str) -> None:
    """Audit a codebase: which Brevitas optimizations are already done, and what is left.

    This is the static scan that audit reports point at ("brevitas audit <repo>");
    it must stay reachable under exactly that name.
    """
    from .audit import main as _audit_main
    _audit_main.callback(path, fmt)


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
              help="AI-assisted pass over files the static scanner can't classify. "
                   "UPLOADS those files' contents (redacted) to YOUR provider — asks first.")
@click.option("--ai-backend", "ai_backend", type=click.Choice(["deepseek", "openai"]), default=None,
              help="Pin which of your providers --ai uploads to (default: whichever key is set).")
def init(path: str, do_apply: bool, use_ai: bool, ai_backend: str | None) -> None:
    """One-command onboarding: find your LLM call sites, wire Brevitas in, start saving.

    Scans the workspace (static analysis; add --ai for tricky codebases), reports every
    call site and provider, checks which API keys are configured locally, and shows the
    two integration paths. Your API keys never leave your machine; with --ai, the
    contents of the unclassified files do go to your own provider (with consent).
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

    # 1b) optional AI fallback on unresolved files — this UPLOADS source, so it is
    # filtered to real project files, redacted downstream, and confirmed first.
    if use_ai:
        from pathlib import Path as _P
        from .scanner.ai_assist import MAX_FILES, ai_classify_files, backend_host
        from .scanner.detector import _iter_python_files

        known = {os.path.realpath(f.path) for f in report.findings}
        known |= {os.path.realpath(c.path) for c in broad.calls}
        candidates: list = []
        for fp in _iter_python_files(path):
            real = os.path.realpath(fp)
            if real in known or any(part.startswith(".") for part in fp.split(os.sep) if part not in (".", "..")):
                continue
            try:
                if os.path.getsize(fp) <= 200:
                    continue
            except OSError:
                continue
            candidates.append(_P(fp))
            if len(candidates) >= MAX_FILES:
                break

        host = backend_host(ai_backend or "")
        if host is None:
            _print("\n[yellow]AI-assisted pass skipped[/yellow] — set OPENAI_API_KEY or "
                   "DEEPSEEK_API_KEY (your key, your account) to enable it.")
            candidates = []
        elif not candidates:
            _print("\n[dim]AI-assisted pass: no unclassified files to submit.[/dim]")
        else:
            _print(f"\n[bold]AI-assisted pass will upload the contents of "
                   f"{len(candidates)} file(s) to [cyan]{host}[/cyan][/bold] "
                   "[dim](your provider account; credentials are redacted first)[/dim]:")
            for p in candidates:
                _print(f"  [cyan]{p}[/cyan]")
            if not click.confirm("Upload these files?", default=False):
                _print("[dim]AI-assisted pass skipped.[/dim]")
                candidates = []

        ai_hits = ai_classify_files(candidates, prefer=ai_backend or "") if candidates else []
        if ai_hits:
            _print(f"\n[bold]AI-assisted pass[/bold] found {len(ai_hits)} more call site(s):")
            for h in ai_hits:
                _print(f"  [cyan]{h['file']}:{h['line']}[/cyan]  {h.get('provider','?')} "
                       f"[dim]{h.get('snippet','')}[/dim]")
        elif candidates:
            _print("\n[dim]AI-assisted pass: nothing additional found.[/dim]")

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
            if written < len(changes):
                _print(f"[yellow]{len(changes) - written} file(s) skipped — changed on disk "
                       "since the scan.[/yellow]")
        else:
            _print("[dim]Skipped. Re-run with --apply when ready.[/dim]")

    _print("\n[bold green]Savings start on your very next call[/bold green] — byte-preserving "
           "caching is automatic. Context-reducing retrieval stays off unless you explicitly "
           "enable it after a paired quality test. Check [yellow]brevitas status[/yellow] "
           "for numbers.\n")


# ── hosted onboarding: `brevitas connect` ────────────────────────────────────
#
# The ONLY billable path is the hosted endpoint reached with an
# `organization_service` key. A device key from `bvx install`, and every receipt
# posted by the local proxy to POST /v1/usage, is recorded authoritative=false by
# the API on purpose, so it can never be billed. `connect` mints the billable key
# type through the EXISTING company-administration endpoint
# (POST /v1/company/service-accounts, api/company_admin.py:1559) — the same one
# the dashboard's Create button posts to. No new auth mechanism, no new endpoint,
# and it never starts a local proxy.

#: Scopes the dashboard already grants a service account (CompanyAdministration.jsx:343).
#: `customer:route` + `customer:auto_provision` are what make per-tenant attribution work.
HOSTED_SERVICE_SCOPES = (
    "proxy:invoke", "usage:write", "usage:read_own",
    "customer:route", "customer:auto_provision",
)
#: The Brevitas credential header. NOT `Authorization` — on the hosted proxy
#: `Authorization` carries YOUR provider key and is forwarded upstream
#: (brevitas/proxy.py:1366,1219), while the Brevitas key is read only from
#: `x-brevitas-key` (api/server.py:1731). Sending it the other way round 401s.
BREVITAS_KEY_HEADER = "X-Brevitas-Key"
#: An organization_service key hard-400s on every proxy call without this
#: (api/server.py:1755-1757). It is the single most likely first-request failure,
#: so every snippet this command prints carries it explicitly.
CUSTOMER_ID_HEADER = "X-Brevitas-Customer-ID"

_CUSTOMER_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_ENV_KEY_LINE = re.compile(r"^\s*(?:export\s+)?BREVITAS_API_KEY\s*=", re.MULTILINE)
_CONNECT_TIMEOUT = 20.0


def _print_unwrapped(message: str) -> None:
    """Print without rich's word wrap: these lines quote paths, headers and API
    detail strings, and a wrap at the console width breaks them mid-token."""
    if _console:
        _console.print(message, soft_wrap=True)
    else:
        print(message)


def _abort(message: str, *hints: str) -> None:
    """Fail with a next step, never a traceback."""
    _print_unwrapped(f"[red]{message}[/red]")
    for hint in hints:
        _print_unwrapped(f"  [dim]{hint}[/dim]")
    sys.exit(1)


def _interactive_stdin() -> bool:
    """True only at a real terminal. CI and pipes must never get a browser window."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _http_request(method: str, url: str, headers: dict, json_body=None):
    """Single seam for every hosted call `connect` makes (tests stub this)."""
    import httpx
    try:
        return httpx.request(method, url, headers=headers, json=json_body,
                             timeout=_CONNECT_TIMEOUT)
    except Exception as exc:                      # network/TLS/DNS — never a secret
        _abort(f"Could not reach {url}: {type(exc).__name__}: {exc}")


def _detail(response) -> str:
    """The API's own error text, bounded and markup-inert.

    Server text reaches _print, which renders rich markup, so square brackets are
    neutralised here rather than letting a detail string colour (or crash) the CLI.
    """
    text = ""
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("detail"):
            text = str(payload["detail"])[:300]
    except Exception:
        pass
    if not text:
        text = (getattr(response, "text", "") or "")[:200]
    return text.replace("[", "(").replace("]", ")")


def _customer_slug(name: str, organization_id: str) -> str:
    """A stable, server-legal external id for the single-tenant case."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (name or "")).strip("-").lower()[:60]
    if slug and _CUSTOMER_EXTERNAL_ID.fullmatch(slug):
        return slug
    fallback = f"org-{(organization_id or '').split('-')[0]}".strip("-")
    return fallback if _CUSTOMER_EXTERNAL_ID.fullmatch(fallback) else "default"


def _device_label() -> str:
    import socket
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname().split(".")[0]).strip("-")
    return (label or "cli")[:40]


def _connection_file():
    from pathlib import Path
    root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(str(Path.home()), ".config")
    return Path(root) / "brevitas" / "connection.json"


def _write_private(path, text: str, *, append: bool = False) -> None:
    """Create/extend a file that only this user can read. 0600 before any bytes land."""
    import stat
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    # An O_CREAT mode only applies to a file this call created; a pre-existing
    # world-readable file keeps its mode, so tighten it unconditionally.
    os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)


def _git(args: list[str], cwd: str) -> tuple[int, str]:
    import subprocess
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=10)
        return done.returncode, (done.stdout or "").strip()
    except Exception:
        return 127, ""


def _guard_env_file(path, force: bool) -> None:
    """Refuse to append a billable secret anywhere it could be committed."""
    directory = str(path.parent if str(path.parent) else ".")
    if path.exists():
        if not path.is_file():
            _abort(f"{path} is not a regular file.")
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _abort(f"Cannot read {path}: {exc}")
        if _ENV_KEY_LINE.search(existing) and not force:
            _abort(f"{path} already defines BREVITAS_API_KEY.",
                   "Re-run with --force to append a replacement line, or edit it yourself.")
    inside, _ = _git(["rev-parse", "--is-inside-work-tree"], directory)
    if inside != 0:
        return                                   # not a git work tree: nothing to commit into
    if _git(["ls-files", "--error-unmatch", "--", str(path)], directory)[0] == 0:
        _abort(f"{path} is tracked by git — refusing to write a service key into it.",
               "Pick an untracked, git-ignored path with --env-file PATH.")
    if _git(["check-ignore", "-q", "--", str(path)], directory)[0] != 0:
        _abort(f"{path} is not matched by .gitignore — refusing to write a service key.",
               f"Add '{path.name}' to .gitignore first, or pass --env-file with an ignored path.")


def _openai_snippet(endpoint: str, customer: str) -> str:
    return f'''import os
from openai import OpenAI

client = OpenAI(
    base_url="{endpoint}",
    api_key=os.environ["OPENAI_API_KEY"],          # your provider key, forwarded upstream
    default_headers={{
        "{BREVITAS_KEY_HEADER}": os.environ["BREVITAS_API_KEY"],
        "{CUSTOMER_ID_HEADER}": "{customer}",
    }},
)'''


def _anthropic_snippet(endpoint: str, customer: str) -> str:
    return f'''import os
from anthropic import Anthropic

client = Anthropic(
    base_url="{endpoint}",
    api_key=os.environ["ANTHROPIC_API_KEY"],       # your provider key, forwarded upstream
    default_headers={{
        "{BREVITAS_KEY_HEADER}": os.environ["BREVITAS_API_KEY"],
        "{CUSTOMER_ID_HEADER}": "{customer}",
    }},
)'''


def _node_snippet(endpoint: str, customer: str) -> str:
    return f'''import OpenAI from "openai";

const client = new OpenAI({{
  baseURL: "{endpoint}",
  apiKey: process.env.OPENAI_API_KEY,            // your provider key, forwarded upstream
  defaultHeaders: {{
    "{BREVITAS_KEY_HEADER}": process.env.BREVITAS_API_KEY,
    "{CUSTOMER_ID_HEADER}": "{customer}",
  }},
}});'''


def _curl_snippet(endpoint: str, customer: str) -> str:
    return f'''curl {endpoint}/chat/completions \\
  -H "{BREVITAS_KEY_HEADER}: $BREVITAS_API_KEY" \\
  -H "{CUSTOMER_ID_HEADER}: {customer}" \\
  -H "Authorization: Bearer $OPENAI_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"model":"gpt-4o-mini","messages":[{{"role":"user","content":"ping"}}]}}\''''


def _env_snippet(endpoint: str, customer: str) -> str:
    return f'''export BREVITAS_API_KEY=…                        # paste the key printed above
export BREVITAS_CUSTOMER_ID={customer}
# Base URL alone is NOT enough on the hosted endpoint: {BREVITAS_KEY_HEADER} and
# {CUSTOMER_ID_HEADER} are headers, and no SDK reads them from the environment.
# Set them on the client (see the snippets above) — not with OPENAI_BASE_URL.'''


_SNIPPETS = {
    "python": ("Python — OpenAI SDK", _openai_snippet),
    "anthropic": ("Python — Anthropic SDK", _anthropic_snippet),
    "node": ("Node — OpenAI SDK", _node_snippet),
    "curl": ("Shell — curl", _curl_snippet),
    "env": ("Shell — environment", _env_snippet),
}


#: The broad scanner costs roughly 8 ms/file. Ordering example snippets is a nicety;
#: it must never turn `connect` into a minute-long scan of a monorepo, so above this
#: many candidate files the default order is used instead.
_DETECT_FILE_BUDGET = 400


def _detected_snippet_order(path: str) -> list[str]:
    """Lead with the SDK actually present, using the in-tree scanner. Never fatal."""
    order = ["python", "anthropic", "node", "curl", "env"]
    try:
        from .scanner.broad import _SKIP_DIRS, analyze_path
    except Exception:
        return order
    seen = 0
    for _root, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        seen += len(filenames)
        if seen > _DETECT_FILE_BUDGET:
            return order
    try:
        providers = {str(call.provider or "").lower() for call in analyze_path(path).calls}
    except Exception:
        return order
    lead = []
    if "anthropic" in providers:
        lead.append("anthropic")
    if providers & {"openai", "deepseek", "groq", "openrouter", "mistral", "xai"}:
        lead.append("python")
    return lead + [name for name in order if name not in lead]


@main.command()
@click.option("--base-url", default=DEFAULT_BASE_URL, envvar="BREVITAS_BASE_URL",
              show_default=True, help="Brevitas API base URL (bare origin, no /v1).")
@click.option("--token", default="", envvar="BREVITAS_DASHBOARD_TOKEN",
              help="Dashboard session token (Supabase access token). Prompted if omitted.")
@click.option("--label", default="", help="Names the service account (default: this hostname).")
@click.option("--customer-id", default="",
              help="Tenant id sent as X-Brevitas-Customer-ID (default: a slug of the org name).")
@click.option("--multi-tenant", is_flag=True,
              help="You route many end customers: print the header as a placeholder and "
                   "import no tenant.")
@click.option("--lang", type=click.Choice(["auto", "python", "anthropic", "node", "curl", "env"]),
              default="auto", show_default=True, help="Which integration snippet to print.")
@click.option("--expires-in-days", type=click.IntRange(1, 365), default=90, show_default=True,
              help="Service-key lifetime. Expiry silently stops your traffic — rotate before it.")
@click.option("--env-file", "env_file", is_flag=False, flag_value=".env", default="",
              metavar="[PATH]",
              help="Opt in to appending the key to an env file (default .env). Refused if the "
                   "path is git-tracked or not git-ignored.")
@click.option("--force", is_flag=True, help="With --env-file: append even if it already sets "
                                            "BREVITAS_API_KEY.")
@click.option("--store-key", is_flag=True, help="Also store the key in your OS keyring.")
@click.option("--dashboard-url", default=DEFAULT_DASHBOARD_URL, envvar="BREVITAS_DASHBOARD_URL",
              show_default=True, help="Where to sign in when prompting for a session token.")
@click.option("--open-browser/--no-open-browser", default=True, show_default=True,
              help="Open the dashboard when a session token has to be prompted for.")
def connect(base_url: str, token: str, label: str, customer_id: str, multi_tenant: bool,
            lang: str, expires_in_days: int, env_file: str, force: bool,
            store_key: bool, dashboard_url: str, open_browser: bool) -> None:
    """Connect this workspace to the hosted Brevitas endpoint — the billable path.

    Mints an organization_service key through the same company-administration
    endpoint the dashboard uses, provisions your tenant id, and prints a
    copy-pasteable client snippet. It never starts a local proxy: local-proxy
    receipts are recorded authoritative=false and cannot be billed.
    """
    import json as _json
    from datetime import datetime
    from pathlib import Path

    check_base_url(base_url)
    base = base_url.rstrip("/")
    api = f"{base}/v1"

    if not token:
        # No new identity concept: the company-administration router accepts one
        # human credential, the dashboard's Supabase session (api/server.py:2066
        # -> _dashboard_identity). Until POST /v1/device-auth/start accepts
        # purpose='hosted_service', that session is what `connect` asks for —
        # attempting today's device-auth exchange here would hand back a DEVICE
        # key, which is silently unbillable, and that is worse than a paste.
        _print_unwrapped("[bold]Sign in to the dashboard, then paste your session "
                         "token.[/bold]")
        _print_unwrapped(f"  [dim]Sign in: {dashboard_url}[/dim]")
        _print_unwrapped("  [dim]The token is the Supabase access token the dashboard "
                         "already sends as Authorization: Bearer[/dim]")
        _print_unwrapped("  [dim](devtools -> Application -> Local Storage -> "
                         "sb-<project-ref>-auth-token -> access_token).[/dim]")
        _print_unwrapped("  [dim]Non-interactive? export BREVITAS_DASHBOARD_TOKEN, or "
                         "pass --token.[/dim]")
        # Only ever hand a browser an http(s) URL: BREVITAS_DASHBOARD_URL is an
        # environment value, and click.launch would happily open file:// or a
        # custom scheme handler with it.
        if (open_browser and _interactive_stdin()
                and dashboard_url.lower().startswith(("https://", "http://"))):
            try:
                click.launch(dashboard_url)
            except Exception:
                pass                              # headless box: the URL is printed above
        token = click.prompt("Dashboard session token", hide_input=True, default="",
                             show_default=False)
    token = token.strip()
    if not token:
        _abort("No dashboard session token given — nothing to authenticate with.")
    try:
        source = click.get_current_context().get_parameter_source("token")
        if source is not None and source.name == "COMMANDLINE":
            _print_unwrapped("[yellow]Note:[/yellow] --token puts a live session token in "
                             "your shell history and the process table.")
            _print_unwrapped("  [dim]Prefer the prompt, or export "
                             "BREVITAS_DASHBOARD_TOKEN.[/dim]")
    except Exception:                             # older click: no parameter sources
        pass

    auth = {"Authorization": f"Bearer {token}"}

    # 1) Who are you, and may you mint a service account here?
    caps = _http_request("GET", f"{api}/company/capabilities", auth)
    if caps.status_code in (401, 403):
        detail = _detail(caps)
        if caps.status_code == 401:
            _abort("That dashboard session is not valid (401).",
                   "Sign in again and copy a fresh token — access tokens expire in an hour.")
        _abort("You are not an active member of any Brevitas organization yet.",
               f"The API said: {detail}",
               "Open the dashboard and create (or accept an invitation to) a workspace, "
               "then run brevitas connect again.")
    if caps.status_code == 503:
        _abort("Company administration is unavailable right now (503).",
               "This is a server-side dependency, not your session. Try again shortly.")
    if caps.status_code != 200:
        _abort(f"Could not read your company profile (HTTP {caps.status_code}).",
               f"The API said: {_detail(caps)}")
    try:
        profile = caps.json()
    except Exception:
        _abort("The API returned a company profile this CLI could not parse.")
    organization_id = str(profile.get("company_id") or "")
    if not organization_id:
        _abort("You are not an active member of any Brevitas organization yet.",
               "Open the dashboard and create (or accept an invitation to) a workspace, "
               "then run brevitas connect again.")
    permissions = set(profile.get("permissions") or [])
    role = str(profile.get("role") or "unknown")
    if "service_accounts:manage" not in permissions:
        _abort(f"Your role ({role}) cannot create service accounts.",
               "Ask a company owner or admin to run brevitas connect for this workspace.")
    organization_name = ""
    for company in profile.get("companies") or []:
        if isinstance(company, dict) and str(company.get("company_id")) == organization_id:
            organization_name = str(company.get("company_name") or "")
            break

    account_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:40] or _device_label()
    tenant = customer_id.strip() or _customer_slug(organization_name, organization_id)
    if not _CUSTOMER_EXTERNAL_ID.fullmatch(tenant):
        _abort(f"--customer-id {tenant!r} is not a legal tenant id.",
               "It must start with a letter or digit, then use only letters, digits, "
               "'.', ':', '-' or '_' — 200 characters at most.")

    # 2) Mint the billable key through the endpoint that already exists.
    created = _http_request("POST", f"{api}/company/service-accounts", auth, {
        "name": f"bvx:{account_label}",
        "environment": "production",
        "scopes": list(HOSTED_SERVICE_SCOPES),
        "expires_in_days": expires_in_days,
    })
    if created.status_code == 409:
        _abort("The API refused to create a service account (409).",
               f"It said: {_detail(created)}",
               "Usual causes: this organization has hit its active service-account limit "
               "(revoke an unused one in Company Administration), or it has no billing "
               "owner recorded yet.")
    if created.status_code in (401, 403):
        _abort(f"Service-account creation denied (HTTP {created.status_code}).",
               f"The API said: {_detail(created)}")
    if created.status_code not in (200, 201):
        _abort(f"Service-account creation failed (HTTP {created.status_code}).",
               f"The API said: {_detail(created)}")
    try:
        account = created.json()
    except Exception:
        _abort("The API returned a service account this CLI could not parse.")
    api_key = str(account.get("api_key") or "")
    if not api_key:
        _abort("The API created a service account but returned no key.",
               "Check Company Administration — you may need to rotate its key to get one.")
    account_id = str(account.get("id") or "")
    expires_at = str(account.get("expires_at") or "")
    expires_day = expires_at[:10] if expires_at else "never"

    # 3) Provision the tenant with the key we just minted — this is also the first
    #    live proof the key works, before the customer's first model call.
    key_header = {BREVITAS_KEY_HEADER: api_key}
    tenant_note = ""
    if multi_tenant:
        tenant_note = "multi-tenant: send your own end-customer id on every request"
    else:
        imported = _http_request("POST", f"{api}/customers/import", key_header,
                                 {"customers": [{"external_id": tenant,
                                                 "display_name": organization_name[:200]}]})
        # A failed import is not fatal: the key is already minted and must still be
        # shown, and the tenant is auto-provisioned on first sight anyway. Say so
        # rather than pretending the row exists.
        if imported.status_code != 200:
            tenant_note = (f"not pre-created (HTTP {imported.status_code}: "
                           f"{_detail(imported)}); created on your first request instead")

    # 4) Persist the non-secret half of the connection.
    connection_path = _connection_file()
    connection = {
        "base_url": base, "endpoint": api, "organization_id": organization_id,
        "organization_name": organization_name, "service_account_id": account_id,
        "key_prefix": str(account.get("prefix") or api_key[:12]),
        "customer_external_id": "" if multi_tenant else tenant,
        "expires_at": expires_at,
        "connected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": account_label,
    }
    try:
        _write_private(connection_path, _json.dumps(connection, indent=2, sort_keys=True) + "\n")
        connection_written = True
    except OSError as exc:
        connection_written = False
        connection_note = f"{type(exc).__name__}: {exc}"

    # 5) Opt-in secret handling. Default: the key exists exactly twice — in the
    #    customer's own secret manager, and hashed in api_keys.
    appended = []
    if env_file:
        env_path = Path(env_file).expanduser()
        _guard_env_file(env_path, force)
        _write_private(env_path, f"\nBREVITAS_API_KEY={api_key}\n"
                                 f"BREVITAS_CUSTOMER_ID={tenant}\n", append=True)
        appended = ["BREVITAS_API_KEY", "BREVITAS_CUSTOMER_ID"]
    keyring_note = ""
    if store_key:
        try:
            import keyring
            keyring.set_password("brevitas:hosted", organization_id, api_key)
            keyring_note = f"stored in your OS keyring as brevitas:hosted / {organization_id}"
        except Exception as exc:
            keyring_note = (f"keyring unavailable ({type(exc).__name__}) — the key was NOT "
                            "stored; keep the value printed above")

    # ── output. click.echo, not rich: these lines get copy-pasted verbatim and
    #    must not be wrapped, coloured, or markup-interpreted.
    display_tenant = "<your-customer-id>" if multi_tenant else tenant
    org_display = organization_name or organization_id
    click.echo("")
    click.echo(f"Connected  {org_display}  ·  org {organization_id}  "
               f"·  service account bvx:{account_label}")
    click.echo(f"Key        {api_key}")
    click.echo("           shown once — this CLI does not store it")
    click.echo(f"Customer   {display_tenant}"
               + (f"   ({tenant_note})" if tenant_note else
                  "   send it on every request"))
    click.echo(f"Endpoint   {api}")
    click.echo(f"Expires    {expires_day}   ({expires_in_days} days — rotate before then)")
    click.echo("")

    names = _detected_snippet_order(".") if lang == "auto" else [lang]
    for name in names:
        title, render = _SNIPPETS[name]
        click.echo(f"# {title}")
        click.echo(render(api, display_tenant))
        click.echo("")

    click.echo(f"{CUSTOMER_ID_HEADER} is mandatory on this key: an organization_service "
               "key without it")
    click.echo("is rejected with 400 \"Organization service proxy calls require "
               f"{CUSTOMER_ID_HEADER}\".")
    if connection_written:
        click.echo(f"Wrote {connection_path} (mode 0600, no secret — org, prefix, expiry only).")
    else:
        click.echo(f"Could not write {connection_path} ({connection_note}); "
                   "nothing else was affected.")
    if appended:
        click.echo(f"Appended to {env_file} (mode 0600): "
                   f"{', '.join(appended)}  — values not echoed.")
    if keyring_note:
        click.echo(keyring_note)
    click.echo("")
    click.echo("Billable: not yet — this workspace has no authoritative usage rows until you "
               "send a")
    click.echo("request through the endpoint above. Then run:  brevitas billing-check")


@main.command("billing-check")
@click.option("--base-url", default="", envvar="BREVITAS_BASE_URL",
              help="Defaults to the endpoint recorded by brevitas connect.")
@click.option("--api-key", default="", envvar="BREVITAS_API_KEY",
              help="Your organization_service key (usage:read_own).")
def billing_check(base_url: str, api_key: str) -> None:
    """Is my traffic actually billable? Reads the org-scoped readiness checklist.

    Exits non-zero unless every check passes, so it can be wired into a smoke test.
    """
    import json as _json

    connection = {}
    path = _connection_file()
    if path.exists():
        try:
            connection = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            connection = {}
    base = (base_url or str(connection.get("base_url") or DEFAULT_BASE_URL)).rstrip("/")
    check_base_url(base)
    if not api_key:
        _abort("No API key. Set BREVITAS_API_KEY to the key from brevitas connect, "
               "or pass --api-key.")
    if connection.get("organization_name"):
        _print(f"[dim]{connection['organization_name']} · org "
               f"{connection.get('organization_id', '')}[/dim]")

    response = _http_request("GET", f"{base}/v1/billing/readiness",
                             {BREVITAS_KEY_HEADER: api_key})
    if response.status_code == 404:
        _abort("This API build does not expose GET /v1/billing/readiness yet.",
               "Until it does, billability is an operator-side query — ask Brevitas.")
    if response.status_code != 200:
        _abort(f"Readiness check failed (HTTP {response.status_code}).",
               f"The API said: {_detail(response)}")
    try:
        payload = response.json()
    except Exception:
        _abort("The API returned a readiness payload this CLI could not parse.")

    for name, check in (payload.get("checks") or {}).items():
        if not isinstance(check, dict):
            continue
        ok = check.get("ok")
        mark = "[green]✓[/green]" if ok else ("[yellow]?[/yellow]" if ok is None else "[red]✗[/red]")
        extra = " ".join(str(check[field]) for field in ("count", "state", "detail")
                         if check.get(field) not in (None, ""))
        _print_unwrapped(f"  {mark} {name}  [dim]{extra.replace('[', '(').replace(']', ')')}[/dim]")
    if payload.get("billable"):
        _print("[green]Billable: yes[/green]")
        return
    _print("[yellow]Billable: no[/yellow] — every failing line above names its own fix.")
    sys.exit(1)


if __name__ == "__main__":
    main()
