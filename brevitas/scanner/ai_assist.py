"""AI-assisted scan fallback (`brevitas init --ai`).

The static scanner handles SDK constructions and known raw-HTTP patterns. For weird
codebases (dynamic client factories, vendored HTTP layers) an LLM pass can classify
what static analysis can't. Strictly opt-in: it needs a provider key (read from the
LOCAL environment — nothing is sent to Brevitas), costs a few cents, and only files
the static pass couldn't classify are submitted (capped).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

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


def _pick_backend() -> Optional[dict]:
    """Cheapest configured OpenAI-compatible backend; keys stay local."""
    if os.environ.get("Deepseek_api_key"):
        return {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                "key": os.environ["Deepseek_api_key"]}
    if os.environ.get("OPENAI_API_KEY"):
        return {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
                "key": os.environ["OPENAI_API_KEY"]}
    return None


def ai_classify_files(paths: List[Path]) -> List[dict]:
    """Classify up to MAX_FILES unresolved files with one LLM call each.
    Returns [{"file", "line", "provider", "how", "snippet"}]; [] if no backend/keys."""
    backend = _pick_backend()
    if backend is None:
        return []
    try:
        import httpx
    except ImportError:
        return []

    findings: List[dict] = []
    for p in paths[:MAX_FILES]:
        try:
            src = p.read_text(errors="replace")[:MAX_CHARS_PER_FILE]
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
