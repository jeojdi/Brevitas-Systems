"""
Brevitas optimization audit — static scanner + CLI.

Answers, for a prospect's repo: which Brevitas optimizations do you ALREADY do
yourself, and what is genuinely left to save? Honesty is the product — every
detector prefers already_implemented / not_applicable / unknown over opportunity
when uncertain, and a fully-optimized repo scores 0 ("well_optimized").

Top-level verdict enum (matches the /v1/audit traffic producer):
"well_optimized" | "moderate_opportunity" | "high_opportunity" | "not_measured".
"well_optimized" requires positive evidence: audits where nothing was verified
(no LLM usage, truncated scans, unknown-diluted low scores) report
"not_measured" instead of a false all-clear.

Usage:
    python -m brevitas.audit <repo_path> [--format json|markdown]

Public API (for the server endpoint and dashboard agents):
    run_audit(path)          -> report dict (schema "brevitas.audit.v1")
    render_markdown(report)  -> str scorecard

Scanned content is treated strictly as data: regex matching only — nothing is
imported, executed, or followed. No network access.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import click

from .audit_capabilities import CAPABILITIES
from .scanner.broad import _PROVIDER_ENDPOINTS, _SDK_PATTERNS, _SKIP_DIRS

SCHEMA = "brevitas.audit.v1"

# .yaml/.yml included: prompt caching is often configured in config files
# (cache_control blocks in prompt yamls) and missing those fabricates an
# opportunity. .json stays excluded — lockfiles/tsconfigs would burn the
# max_files budget for near-zero signal.
_AUDIT_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rb", ".java",
              ".yaml", ".yml"}
_MAX_FILES = 4000
_MAX_FILE_BYTES = 1_000_000
_MAX_EVIDENCE = 5

# Provider-agnostic gateways: seeing only these means we cannot tell which
# upstream provider is hit, so provider-specific capabilities become "unknown".
_GATEWAY_PROVIDERS = {"litellm", "langchain", "openrouter", "any"}

# Violation (negative-polarity) evidence in test/fixture/bench code is not
# billing-relevant and would fabricate opportunities — exclude those paths.
_NOISE_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?|fixtures?|mocks?|examples?|benchmarks?|scratchpad)/"
    r"|(?:^|/)test_|_test\.py$|\.test\.|\.spec\.")

_IMPORT_PATTERNS = [
    ("anthropic", r"^\s*(?:import|from)\s+anthropic\b|@anthropic-ai/sdk"),
    ("openai", r"^\s*(?:import|from)\s+openai\b|require\([\"']openai[\"']\)|from\s+[\"']openai[\"']"),
    ("groq", r"^\s*(?:import|from)\s+groq\b"),
    ("mistral", r"^\s*(?:import|from)\s+mistralai\b"),
    ("cohere", r"^\s*(?:import|from)\s+cohere\b"),
    ("google_gemini", r"google\.generativeai|@google/generative-ai"),
    ("litellm", r"^\s*(?:import|from)\s+litellm\b"),
    ("deepseek", r"deepseek-(?:chat|coder|reasoner)"),
    ("xai", r"grok-[0-9]|xai[-_]sdk"),
]

# OpenAI-compatible SDKs (groq, deepseek, xai, mistral, ...) reuse OpenAI's
# call verbs, so a verb-shape hit alone does not prove openai traffic. When
# openai is detected ONLY via these verbs and a compatible provider is
# confidently detected (import/constructor/endpoint), attributing openai would
# make openai-scoped capabilities "applicable" and fabricate opportunities
# (e.g. pitching cache warming to a Groq-SDK repo).
_OPENAI_VERB_PATTERNS = {
    # verb shapes never name openai; constructor/module patterns do
    pat for prov, pat in _SDK_PATTERNS if prov == "openai" and "penai" not in pat
}
_OPENAI_COMPAT_PROVIDERS = {
    "groq", "deepseek", "xai", "mistral", "together", "fireworks",
    "openrouter", "perplexity", "ollama",
}

# (provider, regex, confident) — confident=False marks ambiguous verb shapes.
_PROVIDER_RES = (
    [(prov, re.compile(pat, re.IGNORECASE), True) for prov, pat in _PROVIDER_ENDPOINTS.items()]
    + [(prov, re.compile(pat), pat not in _OPENAI_VERB_PATTERNS) for prov, pat in _SDK_PATTERNS]
    + [(prov, re.compile(pat, re.MULTILINE), True) for prov, pat in _IMPORT_PATTERNS]
)

_RE_CACHE: dict = {}


def _rx(pattern: str) -> re.Pattern:
    got = _RE_CACHE.get(pattern)
    if got is None:
        got = _RE_CACHE[pattern] = re.compile(pattern, re.IGNORECASE)
    return got


def _iter_files(root: str):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in _AUDIT_EXT:
                yield os.path.join(dirpath, name)


def _read_text(fp: str, max_bytes: int):
    """File text, or None for oversize/binary/unreadable files (never raises)."""
    try:
        if os.path.getsize(fp) > max_bytes:
            return None
        with open(fp, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="ignore")


# Full-line comments (#, //, /* ..., JSDoc continuations). Only consulted for
# negative-polarity (violation) signals: a comment DISCUSSING a volatile value
# is not a billing violation, but skipping comments for positive signals could
# hide real evidence inside minified /*!-prefixed bundles and flip an
# implemented optimization to a false opportunity.
_COMMENT_LINE = re.compile(r"^\s*(?:#|//|/?\*)")


def _scan_signals(cap: dict, key: str, lines, rel: str, out: dict) -> None:
    skip_comments = cap.get("polarity") == "negative" and key == "static_signals"
    for sig in cap.get(key) or []:
        rx = _rx(sig["pattern"])
        exclude = sig.get("exclude")
        for i, line in enumerate(lines, 1):
            m = rx.search(line)
            if not m:
                continue
            if skip_comments and _COMMENT_LINE.match(line):
                continue
            if exclude and _rx(exclude).search(line):
                continue
            out[cap["id"]][key].append({
                "path": rel, "line": i,
                "meaning": sig["meaning"],
                "scope": sig.get("scope", "any"),
                "strength": sig.get("strength", "strong"),
            })


def _scoped(hits, llm_files):
    return [h for h in hits if h["scope"] == "any" or h["path"] in llm_files]


def _evidence(hits):
    seen, out = set(), []
    for h in hits:
        loc = f"{h['path']}:{h['line']}"
        if loc in seen:
            continue
        seen.add(loc)
        out.append(f"{loc} — {h['meaning']}")
    extra = len(out) - _MAX_EVIDENCE
    out = out[:_MAX_EVIDENCE]
    if extra > 0:
        out.append(f"(+{extra} more matches)")
    return out


def _impact(weight: int, verdict: str):
    if verdict == "opportunity":
        return "high" if weight >= 7 else ("medium" if weight >= 4 else "low")
    if verdict == "partial":
        return "medium" if weight >= 7 else "low"
    return None


def _evaluate(cap, providers_detected, llm_any, hits, llm_files, truncated):
    """-> (verdict, evidence, detail)."""
    signals = _scoped(hits["static_signals"], llm_files)
    applicability = _scoped(hits.get("applicability_signals", []), llm_files)

    if not llm_any:
        if truncated:
            return "unknown", [], (
                "Scan was truncated before any LLM usage was seen; absence of evidence is not conclusive."
            )
        return "not_applicable", [], "No LLM API usage detected in the scanned code."

    provs = cap["providers"]
    if provs != "all":
        if not set(provs) & providers_detected:
            if providers_detected & _GATEWAY_PROVIDERS:
                return "unknown", [], (
                    "Only a provider-agnostic gateway was detected; cannot tell whether "
                    f"traffic reaches {'/'.join(provs)}. Not counted as an opportunity."
                )
            if truncated:
                return "unknown", [], (
                    f"Scan was truncated; absence of {'/'.join(provs)} usage in the scanned "
                    "portion is not conclusive."
                )
            return "not_applicable", [], f"No {'/'.join(provs)} usage detected."

    if cap.get("applicability_signals") and not applicability:
        if truncated:
            return "unknown", [], (
                f"Scan was truncated; the precondition "
                f"({cap.get('applicability_meaning', 'precondition')}) may sit in unscanned files."
            )
        return "not_applicable", [], f"Not applicable: {cap.get('applicability_meaning', 'precondition not found')}."

    if cap.get("polarity", "positive") == "negative":
        # Signals are VIOLATIONS (e.g. volatile bytes in prompts); test/fixture
        # code is not billing-relevant evidence.
        signals = [h for h in signals if not _NOISE_PATH.search(h["path"])]
        if signals:
            return "opportunity", _evidence(signals), (
                "Cache-defeating patterns found in prompt construction (static approximation — "
                "confirm against traffic before quoting savings)."
            )
        if truncated:
            return "unknown", [], (
                "Scan was truncated; a clean-prompt claim needs the full codebase."
            )
        return "already_implemented", [], (
            "No cache-defeating patterns found in prompt construction (static approximation)."
        )

    if signals:
        if all(h["strength"] == "weak" for h in signals):
            return "partial", _evidence(signals), "Weak evidence only — likely partially covered; verify before claiming a gap."
        return "already_implemented", _evidence(signals), "Implementation signals found in the codebase."
    if truncated:
        return "unknown", [], "Scan was truncated before completion; absence of evidence is not conclusive."
    return "opportunity", [], "Applicable to detected providers and no implementation signal found."


def run_audit(path: str = ".", max_files: int = _MAX_FILES, max_file_bytes: int = _MAX_FILE_BYTES) -> dict:
    """Statically audit `path` and return the brevitas.audit.v1 report dict."""
    root = os.path.abspath(path)
    provider_hits: dict = {}
    confident_providers: set = set()
    llm_files: set = set()
    cap_hits = {c["id"]: {"static_signals": [], "applicability_signals": []} for c in CAPABILITIES}
    files_scanned, truncated = 0, False

    for fp in _iter_files(root):
        if files_scanned >= max_files:
            truncated = True
            break
        text = _read_text(fp, max_file_bytes)
        if text is None:
            continue
        files_scanned += 1
        rel = os.path.relpath(fp, root) if fp != root else os.path.basename(fp)
        file_is_llm = False
        for prov, rx, confident in _PROVIDER_RES:
            if rx.search(text):
                provider_hits.setdefault(prov, 0)
                provider_hits[prov] += 1
                if confident:
                    confident_providers.add(prov)
                file_is_llm = True
        if file_is_llm:
            llm_files.add(rel)
        lines = None
        for cap in CAPABILITIES:
            for key in ("static_signals", "applicability_signals"):
                if not cap.get(key):
                    continue
                if not any(_rx(s["pattern"]).search(text) for s in cap[key]):
                    continue  # cheap whole-file prefilter
                if lines is None:
                    # split on "\n" only — splitlines() also breaks on \f/\v and
                    # would drift the file:line evidence off by one. Lines are
                    # NOT truncated: the whole-file prefilter above matched, so
                    # capping (e.g. minified single-line bundles with the signal
                    # past the cap) would silently drop the hit and flip an
                    # implemented optimization to a false "opportunity".
                    lines = text.split("\n")
                _scan_signals(cap, key, lines, rel, cap_hits)

    providers_detected = set(provider_hits)
    if ("openai" in providers_detected and "openai" not in confident_providers
            and confident_providers & _OPENAI_COMPAT_PROVIDERS):
        # openai seen only via OpenAI-compatible call verbs while a compatible
        # SDK is confidently present: those verbs belong to that SDK, and
        # claiming openai would fabricate openai-scoped opportunities.
        providers_detected.discard("openai")
    llm_any = bool(providers_detected)

    capabilities, num, den = [], 0.0, 0.0
    gateway_unknowns = False
    for cap in CAPABILITIES:
        verdict, evidence, detail = _evaluate(
            cap, providers_detected, llm_any, cap_hits[cap["id"]], llm_files, truncated)
        if verdict != "not_applicable":
            den += cap["weight"]
            if verdict == "opportunity":
                num += cap["weight"]
            elif verdict == "partial":
                num += cap["weight"] * 0.5
        if verdict == "unknown" and "gateway" in detail:
            gateway_unknowns = True
        capabilities.append({
            "id": cap["id"],
            "name": cap["name"],
            "verdict": verdict,
            "evidence": evidence,
            "estimated_impact": _impact(cap["weight"], verdict),
            "detail": detail,
        })

    score = int(round(100 * num / den)) if den else 0
    # "well_optimized" is a positive claim and needs positive evidence: if
    # nothing was actually measured (no-LLM repo, truncated scan with no
    # signals) the verdict is "not_measured", never a congratulation. A low
    # score diluted by unknown verdicts is likewise not certifiable — the
    # unmeasured capabilities could all be opportunities.
    measured = any(c["verdict"] in ("already_implemented", "partial", "opportunity")
                   for c in capabilities)
    has_unknown = any(c["verdict"] == "unknown" for c in capabilities)
    if not measured:
        verdict = "not_measured"
    elif score >= 60:
        verdict = "high_opportunity"
    elif score >= 25:
        verdict = "moderate_opportunity"
    else:
        verdict = "well_optimized" if not has_unknown else "not_measured"

    caveats = [
        "Static analysis only: dollar estimates require traffic data. No dollar figures are included.",
        "Byte-stability and response-cache verdicts are approximations from source patterns; confirm with a traffic audit before quoting savings.",
    ]
    if not llm_any:
        caveats.append("No LLM API usage was detected, so the audit is not meaningful for this codebase.")
    if truncated:
        caveats.append(f"Scan truncated at {max_files} files; unverified capabilities are reported as unknown, not opportunity.")
    if gateway_unknowns:
        caveats.append("A provider-agnostic gateway is in use; provider-specific capabilities could not be verified statically.")
    if verdict == "not_measured":
        caveats.append(
            "Verdict is not_measured: too little was verified to certify this codebase as "
            "well-optimized — unknown capabilities are unverified, not cleared. Re-run with a "
            "full scan or a traffic audit before quoting a conclusion."
            if measured else
            "Verdict is not_measured: no capability could be verified either way, so no "
            "well-optimized (or opportunity) conclusion may be quoted from this report."
        )

    return {
        "schema": SCHEMA,
        "source": "static",
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "providers_detected": sorted(providers_detected),
        "score": score,
        "verdict": verdict,
        "capabilities": capabilities,
        "caveats": caveats,
    }


_VERDICT_LABEL = {
    "already_implemented": "Already implemented",
    "partial": "Partially implemented",
    "opportunity": "Opportunity",
    "not_applicable": "Not applicable",
    "unknown": "Unknown",
}


def render_markdown(report: dict) -> str:
    lines = [
        "# Brevitas Optimization Audit",
        "",
        f"- Scanned at: {report['scanned_at']}  (source: {report['source']})",
        f"- Providers detected: {', '.join(report['providers_detected']) or 'none'}",
        f"- **Score: {report['score']}/100** (0 = fully optimized) — verdict: **{report['verdict']}**",
        "",
        "| Capability | Verdict | Est. impact |",
        "|---|---|---|",
    ]
    for c in report["capabilities"]:
        lines.append(f"| {c['name']} | {_VERDICT_LABEL[c['verdict']]} | {c['estimated_impact'] or '—'} |")
    lines += ["", "## Detail"]
    for c in report["capabilities"]:
        lines.append(f"- **{c['name']}** — {_VERDICT_LABEL[c['verdict']]}. {c['detail']}")
        for ev in c["evidence"]:
            lines.append(f"    - {ev}")
    lines += ["", "## Caveats"]
    lines += [f"- {c}" for c in report["caveats"]]
    return "\n".join(lines) + "\n"


@click.command()
@click.argument("path", default=".", required=False)
@click.option("--format", "fmt", type=click.Choice(["json", "markdown"]), default="markdown",
              show_default=True, help="Output format.")
def main(path: str, fmt: str) -> None:
    """Audit a codebase: which Brevitas optimizations are already done, and what is left."""
    report = run_audit(path)
    if fmt == "json":
        click.echo(json.dumps(report, indent=2))
    else:
        click.echo(render_markdown(report))


if __name__ == "__main__":
    main()
