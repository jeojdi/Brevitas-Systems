"""
Repo scanner: walk a codebase, flag every LLM/AI API call site (any language),
and derive the one-click routing config that sends them through Brevitas.

Detection is regex over source lines using the provider registry in
`signatures.py` — endpoint hosts + call methods catch calls in Go, Rust, PHP,
Ruby, Java, shell/curl, etc., not just the Python/JS SDKs.

    python -m brevitas.scan.scanner        # runs the self-check
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .signatures import ENDPOINT, CALL, PROVIDERS_BY_ID, iter_patterns

# Dirs we never scan — vendored deps, VCS, build output. ponytail: hardcoded skip
# list beats parsing .gitignore; add gitignore support when a repo layout needs it.
_SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", "vendor", "target",
    ".vercel", "site-packages", ".idea", ".vscode",
}
# Binary / non-source extensions to skip outright.
_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip",
    ".gz", ".tar", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".lock",
    ".map", ".min.js", ".wasm", ".so", ".dylib", ".bin", ".db",
    ".md", ".rst",  # prose docs — never a runtime call site, only noise
}
_MAX_BYTES = 1_000_000  # skip files larger than 1 MB


@dataclass
class Finding:
    path: str
    line: int
    provider: str          # provider id
    provider_name: str
    kind: str              # endpoint | call | import | model
    snippet: str


def scan(root: str | Path) -> list[Finding]:
    """Scan `root` recursively and return one Finding per (file, line, provider)."""
    root = Path(root)
    patterns = list(iter_patterns())
    findings: list[Finding] = []
    for path in _walk(root):
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            seen: set[str] = set()  # dedup per provider per line
            for provider, pat in patterns:
                if provider.id in seen:
                    continue
                m = pat.regex.search(raw)
                if m:
                    seen.add(provider.id)
                    findings.append(Finding(
                        path=str(path), line=lineno, provider=provider.id,
                        provider_name=provider.name, kind=pat.kind,
                        snippet=line[:160],
                    ))
    return findings


def _walk(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in _SKIP_EXT or path.name.endswith(".min.js"):
            continue
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
        except OSError:
            continue
        yield path


def call_sites(findings: list[Finding]) -> list[Finding]:
    """Findings that are actual call sites (endpoint or call method), highest signal."""
    return [f for f in findings if f.kind in (ENDPOINT, CALL)]


def providers_found(findings: list[Finding]) -> list[str]:
    """Provider ids present, ordered by first appearance."""
    out: list[str] = []
    for f in findings:
        if f.provider not in out:
            out.append(f.provider)
    return out


def routing(findings: list[Finding], proxy: str = "http://localhost:4242") -> dict:
    """
    One-click routing plan. OpenAI- and Anthropic-SDK calls redirect with a
    single env var each (the proxy serves both). OpenAI-compatible providers
    (deepseek, groq, …) ride the same OPENAI_BASE_URL when called through the
    OpenAI SDK. Everyone else needs a manual base_url change.
    """
    env: dict[str, str] = {}
    auto: list[str] = []
    manual: list[str] = []
    for pid in providers_found(findings):
        spec = PROVIDERS_BY_ID[pid]
        if pid == "anthropic":
            env["ANTHROPIC_BASE_URL"] = proxy
            auto.append(pid)
        elif pid == "openai" or spec.openai_compatible:
            env["OPENAI_BASE_URL"] = f"{proxy}/openai"
            auto.append(pid)
        else:
            manual.append(pid)  # google, cohere, bedrock, replicate, hf, langchain, azure
    return {"env": env, "auto": auto, "manual": manual, "proxy": proxy}


def _route_target(spec, proxy: str) -> str | None:
    """Proxy base URL a hardcoded site should point at, or None if not auto-routable."""
    if spec.id == "anthropic":
        return proxy                       # SDK appends /v1/messages
    if spec.id == "openai" or spec.openai_compatible:
        return f"{proxy}/openai"           # SDK appends /chat/completions
    return None                            # google/cohere/bedrock/hf/… → manual


def hardcoded_sites(findings: list[Finding]) -> list[Finding]:
    """
    ENDPOINT findings for auto-routable providers = literal provider URLs in
    source (e.g. base_url="https://api.openai.com/v1"). These bypass the env-var
    redirect, so they're what `--auto` rewrites (or we flag for a manual edit).
    """
    return [f for f in findings
            if f.kind == ENDPOINT and _route_target(PROVIDERS_BY_ID[f.provider], "") is not None]


def apply_autofix(findings: list[Finding], proxy: str = "http://localhost:4242") -> list[tuple[str, int, str]]:
    """
    Rewrite each hardcoded provider URL to the Brevitas proxy, in place.
    Only touches the exact flagged line and only the `https://<host>…` literal on
    it — never comments elsewhere. Returns (path, line, new_line) for each edit.
    Skips .md so docs aren't rewritten.
    """
    edits: list[tuple[str, int, str]] = []
    by_file: dict[str, list[Finding]] = {}
    for f in hardcoded_sites(findings):
        by_file.setdefault(f.path, []).append(f)

    for path, fs in by_file.items():
        p = Path(path)
        if p.suffix.lower() == ".md":
            continue
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        changed = False
        for f in fs:
            spec = PROVIDERS_BY_ID[f.provider]
            target = _route_target(spec, proxy)
            endpoint_pat = next((pt for pt in spec.patterns if pt.kind == ENDPOINT), None)
            if target is None or endpoint_pat is None or f.line > len(lines):
                continue
            url_re = re.compile(r"https?://" + endpoint_pat.regex.pattern + r"""[^\s'"`)]*""",
                                re.IGNORECASE)
            before = lines[f.line - 1]
            after = url_re.sub(target, before)
            if after != before:
                lines[f.line - 1] = after
                edits.append((path, f.line, after.strip()))
                changed = True
        if changed:
            p.write_text("".join(lines), encoding="utf-8")
    return edits


# ── self-check ────────────────────────────────────────────────────────────────

def _selfcheck() -> None:
    import tempfile
    fixtures = {
        "app.py": "resp = client.chat.completions.create(model='gpt-4o', messages=m)\n",
        "bot.ts": "const r = await anthropic.messages.create({ model: 'claude-3-5' })\n",
        "main.go": 'req, _ := http.NewRequest("POST", "https://api.deepseek.com/v1/chat/completions", body)\n',
        "svc.rb": 'uri = URI("https://generativelanguage.googleapis.com/v1/models")\n',
        "hard.py": 'client = OpenAI(base_url="https://api.openai.com/v1", api_key=k)\n',
        "readme.md": "just prose, no api calls here\n",
    }
    with tempfile.TemporaryDirectory() as d:
        for name, body in fixtures.items():
            (Path(d) / name).write_text(body)
        f = scan(d)
        found = providers_found(f)
        assert "openai" in found, found
        assert "anthropic" in found, found
        assert "deepseek" in found, found          # Go, host-only match
        assert "google_gemini" in found, found     # Ruby, host-only match
        plan = routing(f)
        assert plan["env"]["OPENAI_BASE_URL"].endswith("/openai"), plan
        assert plan["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:4242", plan
        assert "google_gemini" in plan["manual"], plan  # no env redirect for Gemini

        # hardcoded URL detection + --auto rewrite
        assert any(h.path.endswith("hard.py") for h in hardcoded_sites(f)), "missed hardcoded"
        edits = apply_autofix(f)
        hardpy = (Path(d) / "hard.py").read_text()
        assert "api.openai.com" not in hardpy, hardpy            # literal rewritten
        assert "http://localhost:4242/openai" in hardpy, hardpy  # → proxy
        assert edits, edits
    print("ok: detected", found, "| autofixed", len(edits))


if __name__ == "__main__":
    _selfcheck()
