"""AI-assisted scan fallback (`brevitas init --ai`).

The static scanner handles SDK constructions and known raw-HTTP patterns. For weird
codebases (dynamic client factories, vendored HTTP layers) an LLM pass can classify
what static analysis can't. Strictly opt-in: it needs a provider key (read from the
LOCAL environment — nothing is sent to Brevitas), costs a few cents, and only files
the static pass couldn't classify are submitted (capped).

Be precise about what this does: the *contents* of those files are uploaded to YOUR
configured provider (DeepSeek or OpenAI). Keys stay local, source does not — so the
caller must obtain consent first (`brevitas init --ai` prompts), and every byte is
passed through ``security.redaction.redact_text`` before it leaves the machine.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from ..security.redaction import redact_text

MAX_FILES = 5
MAX_CHARS_PER_FILE = 8_000

_PROMPT = """You are a static-analysis assistant. For the given source file, list every
place an LLM/chat-completion API is called (any provider: OpenAI, Anthropic, DeepSeek,
Groq, Gemini, or a raw HTTP call to such an API). Respond ONLY with a JSON array; each
item: {"line": <int>, "provider": "<best guess>", "how": "<sdk|http|other>",
"snippet": "<the call expression, <=80 chars>"}. Empty array if none.

FILE: %s
```
%s
```"""


_BACKENDS = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                 "envs": ("DEEPSEEK_API_KEY", "Deepseek_api_key")},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "envs": ("OPENAI_API_KEY",)},
}


def _pick_backend(prefer: str = "") -> Optional[dict]:
    """Cheapest configured OpenAI-compatible backend; keys stay local.

    ``prefer`` (from ``--ai-backend``) pins the destination, so the upload target is
    never chosen silently on the customer's behalf.
    """
    order = [prefer] if prefer else ["deepseek", "openai"]
    for name in order:
        spec = _BACKENDS.get(name)
        if spec is None:
            continue
        key = next((os.environ[e] for e in spec["envs"] if os.environ.get(e)), None)
        if key:
            return {"name": name, "base_url": spec["base_url"], "model": spec["model"], "key": key}
    return None


def backend_host(prefer: str = "") -> Optional[str]:
    """Destination the next upload would go to, for the consent prompt. None if unconfigured."""
    backend = _pick_backend(prefer)
    return None if backend is None else backend["base_url"]


def _redact_source(text: str) -> str:
    """Strip credentials from source before it leaves the machine.

    Line-by-line, and with the original indentation restored, because ``redact_text``
    drops newlines and tabs — and the model is asked to report line numbers.
    """
    out = []
    for line in text.splitlines():
        body = line.lstrip()
        indent = line[: len(line) - len(body)]
        out.append(indent + redact_text(body, maximum=4096))
    return "\n".join(out)


def ai_classify_files(paths: List[Path], *, prefer: str = "") -> List[dict]:
    """Classify up to MAX_FILES unresolved files with one LLM call each.

    File contents leave the machine (redacted) for the configured provider — callers
    must have taken consent before calling this.
    Returns [{"file", "line", "provider", "how", "snippet"}]; [] if no backend/keys."""
    backend = _pick_backend(prefer)
    if backend is None:
        return []
    try:
        import httpx
    except ImportError:
        return []

    findings: List[dict] = []
    for p in paths[:MAX_FILES]:
        try:
            src = _redact_source(p.read_text(errors="replace")[:MAX_CHARS_PER_FILE])
        except OSError:
            continue
        try:
            r = httpx.post(
                f"{backend['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {backend['key']}"},
                json={"model": backend["model"], "max_tokens": 500, "temperature": 0,
                      "messages": [{"role": "user", "content": _PROMPT % (p.name, src)}]},
                timeout=30,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            start, end = text.find("["), text.rfind("]") + 1
            if start < 0 or end <= start:
                continue
            for item in json.loads(text[start:end]):
                if isinstance(item, dict) and item.get("line"):
                    findings.append({"file": str(p), **item})
        except Exception:
            continue  # best-effort: AI assist never breaks onboarding
    return findings
