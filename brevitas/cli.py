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


def _alive(url: str) -> bool:
    import httpx
    try:
        return httpx.get(url, timeout=1).status_code == 200
    except Exception:
        return False


def _local_key(base_url: str) -> str:
    """Reuse the saved local key, or mint one. Kept at ~/.brevitas/key (chmod 600)."""
    import httpx
    from pathlib import Path
    kp = Path.home() / ".brevitas" / "key"
    if kp.exists():
        k = kp.read_text().strip()
        if k and _alive_key(base_url, k):
            return k
    r = httpx.post(f"{base_url}/v1/keys", json={"name": "local"}, timeout=5)
    k = r.json()["api_key"]
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_text(k)
    kp.chmod(0o600)
    return k


def _alive_key(base_url: str, key: str) -> bool:
    import httpx
    try:
        return httpx.get(f"{base_url}/v1/stats", headers={"X-API-Key": key}, timeout=3).status_code == 200
    except Exception:
        return False


@main.command()
@click.option("--port",     default=4242,                    show_default=True, help="Proxy listen port")
@click.option("--api-key",  default="",  envvar="BREVITAS_API_KEY",            help="Brevitas key (auto-created locally if unset)")
@click.option("--base-url", default="http://localhost:8000", envvar="BREVITAS_BASE_URL", show_default=True, help="Compression engine URL")
@click.option("--host",     default="127.0.0.1",             show_default=True, help="Bind host")
@click.option("--engine/--no-engine", default=True, help="Auto-start the local compression engine (off if pointing at a remote one)")
def start(port: int, api_key: str, base_url: str, host: str, engine: bool) -> None:
    """Start the whole local stack — compression engine + proxy, self-configured."""
    import atexit
    import subprocess
    import time
    from pathlib import Path
    from urllib.parse import urlparse

    repo_root = Path(__file__).resolve().parent.parent
    is_local = urlparse(base_url).hostname in ("localhost", "127.0.0.1")
    bport = urlparse(base_url).port or 8000

    # 1) Bring up the compression engine (unless it's already up, or remote).
    if engine and is_local and not _alive(f"{base_url}/v1/health"):
        _print(f"[dim]starting compression engine on :{bport} …[/dim]")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api.server:app",
             "--host", host, "--port", str(bport), "--log-level", "warning"],
            cwd=str(repo_root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        atexit.register(proc.terminate)
        for _ in range(40):
            if _alive(f"{base_url}/v1/health"):
                break
            time.sleep(0.25)
        if not _alive(f"{base_url}/v1/health"):
            _print("[red]✗ compression engine didn't come up — is this the repo root?[/red]")
            sys.exit(1)

    # 2) Get a working key (reuse or auto-mint) and self-configure.
    if not api_key:
        if _alive(f"{base_url}/v1/health"):
            api_key = _local_key(base_url)
        else:
            _print("[yellow]No engine reachable and no --api-key; savings won't be tracked.[/yellow]")
    os.environ["BREVITAS_API_KEY"]  = api_key
    os.environ["BREVITAS_BASE_URL"] = base_url
    from . import configure
    configure(api_key=api_key, base_url=base_url)

    _print(f"\n[bold green]Brevitas proxy ready on {host}:{port}[/bold green]  [dim](engine {base_url})[/dim]")
    _print("[dim]Point your app here (or run `brevitas install`):[/dim]")
    _print(f"  [yellow]ANTHROPIC_BASE_URL=http://{host}:{port}[/yellow]")
    _print(f"  [yellow]OPENAI_BASE_URL=http://{host}:{port}/openai[/yellow]\n")

    try:
        import uvicorn
        from .proxy import proxy_app
        uvicorn.run(proxy_app, host=host, port=port, log_level="warning")
    except ImportError:
        _print("[red]uvicorn not installed. Run: pip install brevitas-systems[/red]")
        sys.exit(1)


@main.command(name="scan")
@click.argument("path", default=".")
@click.option("--target", default="http://localhost:4242", show_default=True, help="Brevitas proxy URL")
@click.option("--popup/--no-popup", default=True, help="Open a visual popup of the API calls found")
def scan_cmd(path: str, target: str, popup: bool) -> None:
    """Find every AI API call site in PATH and show the routing plan."""
    from collections import Counter
    from .scan import scan, call_sites, providers_found, routing, hardcoded_sites

    findings = scan(path)
    calls = call_sites(findings)
    if not calls:
        _print("[yellow]No AI API call sites found.[/yellow]")
        return

    counts = Counter(f.provider for f in calls)
    plan = routing(findings, proxy=target)
    hard = hardcoded_sites(findings)

    _print(f"\n[bold]{len(calls)} call sites[/bold] across [bold]{len(counts)}[/bold] providers:")
    for pid, n in counts.most_common():
        tag = "auto" if pid in plan["auto"] else "manual"
        _print(f"  {n:>4}  {pid:<16} [dim]({tag})[/dim]")

    _print("\n[bold]Routing[/bold] — set these to send calls through Brevitas:")
    for k, v in plan["env"].items():
        _print(f"  [yellow]{k}={v}[/yellow]")
    if plan["manual"]:
        _print(f"  [dim]manual (own SDK, edit base_url): {', '.join(plan['manual'])}[/dim]")
    if hard:
        _print(f"\n[bold red]{len(hard)} hardcoded URL(s)[/bold red] the env vars can't override:")
        for f in hard[:10]:
            _print(f"  {f.path}:{f.line}  [dim]{f.snippet}[/dim]")
        if len(hard) > 10:
            _print(f"  [dim]… +{len(hard) - 10} more[/dim]")
        _print("  [dim]Run `brevitas install --auto` to rewrite them.[/dim]")

    if popup:
        out = _render_and_open(path, findings, plan)
        _print(f"\n[green]✓ Popup:[/green] {out}  [dim](--no-popup to skip)[/dim]")
    _print("")


@main.command(name="install")
@click.argument("path", default=".")
@click.option("--target", default="http://localhost:4242", show_default=True, help="Brevitas proxy URL")
@click.option("--auto", is_flag=True, help="Rewrite hardcoded provider URLs in place (edits your files)")
@click.option("--env-file", default=".env.brevitas", show_default=True, help="Where to write the routing env vars")
def install_cmd(path: str, target: str, auto: bool, env_file: str) -> None:
    """Wire PATH to route through Brevitas: write routing env vars + handle hardcoded URLs."""
    from pathlib import Path
    from .scan import scan, routing, hardcoded_sites, apply_autofix

    findings = scan(path)
    plan = routing(findings, proxy=target)
    if not plan["env"] and not hardcoded_sites(findings):
        _print("[yellow]Nothing to route — no OpenAI/Anthropic-compatible calls found.[/yellow]")
        return

    env_lines = ["# Brevitas routing — `source` this before running your app\n"]
    env_lines += [f"export {k}={v}\n" for k, v in plan["env"].items()]
    Path(env_file).write_text("".join(env_lines))
    _print(f"\n[green]✓ Wrote {env_file}[/green]  →  [dim]source {env_file}[/dim]")
    for k, v in plan["env"].items():
        _print(f"    [yellow]{k}={v}[/yellow]")

    if plan["manual"]:
        _print(f"\n[bold]Manual:[/bold] these use their own SDK — point base_url at {target}:")
        _print(f"    [dim]{', '.join(plan['manual'])}[/dim]")

    hard = hardcoded_sites(findings)
    if auto:
        edits = apply_autofix(findings, proxy=target)
        _print(f"\n[green]✓ Rewrote {len(edits)} hardcoded URL(s)[/green]")
        for pth, line, new in edits[:20]:
            _print(f"    {pth}:{line}  [dim]{new}[/dim]")
    elif hard:
        _print(f"\n[bold red]{len(hard)} hardcoded URL(s)[/bold red] — env vars can't override these. Edit or re-run with [bold]--auto[/bold]:")
        for f in hard[:20]:
            _print(f"    {f.path}:{f.line}  [dim]{f.snippet}[/dim]")

    _print(f"\n[dim]Then: brevitas start  (proxy on {target})[/dim]\n")


_DASH_CSS = """
* { box-sizing: border-box; }
body { margin:0; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
       background:#0b0f14; color:#d7dee8; }
.wrap { max-width:1000px; margin:0 auto; padding:32px 20px 64px; }
h1 { font-size:20px; margin:0 0 4px; color:#fff; }
.sub { color:#7c8794; margin:0 0 24px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:0 0 28px; }
.card { background:#131a23; border:1px solid #1f2a37; border-radius:10px; padding:16px; }
.card .n { font-size:26px; font-weight:700; color:#fff; }
.card .l { color:#7c8794; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
.card.save .n { color:#4ade80; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:#7c8794;
     border-bottom:1px solid #1f2a37; padding-bottom:8px; margin:32px 0 14px; }
.row { display:flex; align-items:center; gap:10px; margin:6px 0; }
.row .name { width:150px; color:#d7dee8; }
.bar { height:16px; background:#2563eb; border-radius:3px; min-width:2px; }
.bar.manual { background:#f59e0b; }
.row .c { color:#7c8794; width:70px; }
.tag { font-size:11px; padding:1px 7px; border-radius:99px; }
.tag.auto { background:#14351f; color:#4ade80; }
.tag.manual { background:#3a2a10; color:#f59e0b; }
pre { background:#0f151d; border:1px solid #1f2a37; border-radius:8px; padding:12px 14px;
      overflow-x:auto; color:#9fd0ff; }
code.f { color:#7c8794; }
.file { margin:10px 0 14px; }
.fp { color:#fff; font-weight:600; margin-bottom:2px; word-break:break-all; }
.hard { color:#f87171; }
.empty { color:#7c8794; font-style:italic; }
a { color:#60a5fa; }
"""


def _group_by_file(calls):
    """[(path, [(line, provider, snippet), …]), …] ordered by call count desc."""
    from collections import defaultdict
    d = defaultdict(list)
    for f in calls:
        d[f.path].append((f.line, f.provider, f.snippet))
    for v in d.values():
        v.sort()
    return sorted(d.items(), key=lambda kv: len(kv[1]), reverse=True)


def _render_and_open(path, findings, plan, stats=None):
    """Write the visual report to a temp HTML file and open it. Returns the path."""
    import tempfile, webbrowser
    from collections import Counter
    from pathlib import Path
    from .scan import call_sites, hardcoded_sites
    calls = call_sites(findings)
    counts = Counter(f.provider for f in calls)
    html = _dash_html(path, counts, plan, hardcoded_sites(findings), stats,
                      by_file=_group_by_file(calls))
    out = Path(tempfile.gettempdir()) / "brevitas_scan.html"
    out.write_text(html, encoding="utf-8")
    webbrowser.open(f"file://{out}")
    return out


def _dash_html(path, counts, plan, hard, stats, by_file=None):
    import html as _html
    mx = max(counts.values()) if counts else 1
    rows = ""
    for pid, n in counts.most_common():
        auto = pid in plan["auto"]
        rows += (f'<div class="row"><span class="name">{_html.escape(pid)}</span>'
                 f'<span class="bar {"" if auto else "manual"}" style="width:{int(n/mx*300)}px"></span>'
                 f'<span class="c">{n}</span>'
                 f'<span class="tag {"auto" if auto else "manual"}">{"routed" if auto else "manual"}</span></div>')

    # Which files make API calls, and where (file:line + the calling line).
    files_html = ""
    for fpath, items in (by_file or {}):
        lines = "".join(
            f'<div class="row"><span class="c" style="width:52px">L{ln}</span>'
            f'<span class="tag {"auto" if pid in plan["auto"] else "manual"}">{_html.escape(pid)}</span> '
            f'<code class="f">{_html.escape(snip[:100])}</code></div>'
            for ln, pid, snip in items)
        files_html += f'<div class="file"><div class="fp">{_html.escape(fpath)}</div>{lines}</div>'
    files_html = files_html or '<div class="empty">no AI calls found</div>'
    env_block = "\n".join(f"export {k}={v}" for k, v in plan["env"].items()) or "# no OpenAI/Anthropic-compatible calls found"
    hard_html = ("".join(
        f'<div class="row"><span class="hard">{_html.escape(h.path)}:{h.line}</span> '
        f'<code class="f">{_html.escape(h.snippet[:90])}</code></div>' for h in hard[:15])
        or '<div class="empty">none — every call routes via env vars</div>')

    if stats and stats.get("total_calls"):
        saved = stats.get("total_tokens_saved") or (stats.get("total_baseline_tokens", 0) - stats.get("total_optimized_tokens", 0))
        save_cards = (
            f'<div class="card save"><div class="n">{saved:,}</div><div class="l">tokens saved</div></div>'
            f'<div class="card save"><div class="n">${stats.get("total_cost_saved_usd",0):.4f}</div><div class="l">cost saved</div></div>'
            f'<div class="card"><div class="n">{stats.get("avg_savings_pct",0):.1f}%</div><div class="l">avg savings</div></div>'
            f'<div class="card"><div class="n">{stats.get("total_calls",0)}</div><div class="l">calls routed</div></div>')
        savings_section = f'<h2>Live savings (through the proxy)</h2><div class="cards">{save_cards}</div>'
    else:
        savings_section = ('<h2>Live savings</h2><div class="empty">No calls routed yet. '
                           'Run <code>brevitas start</code>, <code>source .env.brevitas</code>, then use your app.</div>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Brevitas — {_html.escape(str(path))}</title><style>{_DASH_CSS}</style></head><body><div class="wrap">
<h1>Brevitas backend</h1><p class="sub">scanned <code>{_html.escape(str(path))}</code></p>
<div class="cards">
  <div class="card"><div class="n">{sum(counts.values())}</div><div class="l">AI call sites</div></div>
  <div class="card"><div class="n">{len(counts)}</div><div class="l">providers</div></div>
  <div class="card"><div class="n">{len(hard)}</div><div class="l">hardcoded URLs</div></div>
</div>
{savings_section}
<h2>Providers found</h2>{rows or '<div class="empty">no AI calls found</div>'}
<h2>Which files make API calls (and where)</h2>{files_html}
<h2>Routing — set these to send calls through Brevitas</h2><pre>{_html.escape(env_block)}</pre>
<h2>Hardcoded URLs (need --auto or a manual edit)</h2>{hard_html}
</div></body></html>"""


@main.command(name="dash")
@click.argument("path", default=".")
@click.option("--target", default="http://localhost:4242", show_default=True, help="Brevitas proxy URL")
@click.option("--api", default="http://localhost:8000", show_default=True, help="Compression API (for live savings)")
@click.option("--api-key", default="", envvar="BREVITAS_API_KEY", help="Brevitas key, to pull live savings")
@click.option("--open/--no-open", "open_", default=True, help="Open the page in a browser")
def dash_cmd(path: str, target: str, api: str, api_key: str, open_: bool) -> None:
    """Generate a local HTML view of the AI calls in PATH + live savings."""
    import json, tempfile, webbrowser
    from collections import Counter
    from pathlib import Path
    from .scan import scan, call_sites, routing, hardcoded_sites

    findings = scan(path)
    counts = Counter(f.provider for f in call_sites(findings))
    plan = routing(findings, proxy=target)
    hard = hardcoded_sites(findings)

    stats = None
    if api_key:
        try:
            import httpx
            r = httpx.get(f"{api}/v1/stats", headers={"X-API-Key": api_key}, timeout=3)
            if r.status_code == 200:
                stats = r.json()
        except Exception:
            pass  # backend not running → scan-only view

    out = Path(tempfile.gettempdir()) / "brevitas_dashboard.html"
    out.write_text(_dash_html(path, counts, plan, hard, stats,
                              by_file=_group_by_file(call_sites(findings))), encoding="utf-8")
    _print(f"[green]✓ Dashboard:[/green] {out}")
    if not stats:
        _print("[dim](no live savings — pass --api-key and start the backend to include them)[/dim]")
    if open_:
        webbrowser.open(f"file://{out}")


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
