"""`brevitas chat` — chat with a big document, with live token tracking.

The big document is pinned as the system context and re-sent every turn (like Claude Code
keeping a codebase in context). Brevitas keeps that prefix byte-identical so the provider
caches it: turn 1 pays full price (cache write), turns 2+ read the doc from cache at a deep
discount. You watch the per-turn + cumulative token/cost savings.
"""

from __future__ import annotations

import os
from typing import List, Optional

from .resource_bounds import (
    ResourceBounds,
    ResourceLimitExceeded,
    extend_bounded_list,
    require_size,
    safe_close_resource,
    utf8_size,
)


def read_document(path: str, *, max_bytes: int | None = None) -> str:
    """Read a document as text. Supports PDF (textbooks/papers) and plain text/markdown."""
    limit = max_bytes or ResourceBounds.from_env().demo_document_max_bytes
    limit = max(1, min(int(limit), 32 * 1024 * 1024))
    if path.lower().endswith(".pdf"):
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(path)
            parts: list[str] = []
            total = 0
            for page in doc:
                text = page.get_text()
                total += utf8_size(text) + (1 if parts else 0)
                if total > limit:
                    raise ResourceLimitExceeded(f"document exceeds {limit} bytes")
                parts.append(text)
            return "\n".join(parts)
        except ImportError:
            pass
        try:
            from pypdf import PdfReader
            parts = []
            total = 0
            for page in PdfReader(path).pages:
                text = page.extract_text() or ""
                total += utf8_size(text) + (1 if parts else 0)
                if total > limit:
                    raise ResourceLimitExceeded(f"document exceeds {limit} bytes")
                parts.append(text)
            return "\n".join(parts)
        except ImportError:
            raise SystemExit(
                "Reading PDFs needs a PDF library. Install one:\n"
                "  pip install \"brevitas-systems[pdf]\"   (or: pip install pymupdf)")
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ResourceLimitExceeded(f"document exceeds {limit} bytes")
    return raw.decode("utf-8", errors="ignore")


def _load_key(provider: str, explicit: str) -> str:
    if explicit:
        return explicit
    names = {
        "deepseek": ["DEEPSEEK_API_KEY", "Deepseek_api_key"],
        "openai": ["OPENAI_API_KEY"],
        "anthropic": ["ANTHROPIC_API_KEY"],
    }.get(provider, [])
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    # try .env / .env.local in CWD
    for fn in (".env.local", ".env"):
        if os.path.exists(fn):
            for line in open(fn):
                for n in names:
                    if line.startswith(n + "="):
                        return line.split("=", 1)[1].strip()
    return ""


def _base_url(provider: str) -> str:
    return {
        "deepseek": "https://api.deepseek.com/v1",
        "openai": "https://api.openai.com/v1",
    }.get(provider, "https://api.openai.com/v1")


def run_chat(doc_path: str, provider: str = "deepseek", model: str = "",
             api_key: str = "", printer=print) -> None:
    from brevitas import BrevitasClient
    from token_efficiency_model.lossless.provider_cache import count_tokens

    bounds = ResourceBounds.from_env()
    document = read_document(doc_path, max_bytes=bounds.demo_document_max_bytes)
    doc_tokens = count_tokens(document)

    key = _load_key(provider, api_key)
    if not key:
        printer(f"No API key for {provider}. Set its env var or pass --api-key.")
        return
    model = model or {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini",
                      "anthropic": "claude-sonnet-4-6"}.get(provider, "gpt-4o-mini")

    client = BrevitasClient(provider=provider, api_key=key, base_url=_base_url(provider))
    system = ("Answer questions using the following document as the source of truth.\n\n"
              "=== DOCUMENT ===\n" + document + "\n=== END DOCUMENT ===")
    try:
        _run_interactive_chat(
            client, doc_path, provider, model, system, doc_tokens, bounds, printer
        )
    finally:
        safe_close_resource(client)


def _run_interactive_chat(client, doc_path: str, provider: str, model: str,
                          system: str, doc_tokens: int, bounds: ResourceBounds,
                          printer) -> None:

    printer(f"\nLoaded '{doc_path}' as context: ~{doc_tokens:,} tokens.")
    printer(f"Provider: {provider}/{model}. The document is re-sent each turn; the provider "
            f"caches it so turns 2+ are cheaper. Type a question (or 'exit').\n")

    history: List[dict] = []
    turn = 0
    cum_uncached = cum_actual = cum_cached = 0.0

    while True:
        try:
            q = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            printer("")
            break
        if not q or q.lower() in ("exit", "quit", ":q"):
            break
        try:
            require_size(q, bounds.session_max_item_bytes, name="question", sizer=utf8_size)
        except ResourceLimitExceeded as exc:
            printer(f"  [error: {exc}]")
            continue
        turn += 1

        messages = [{"role": "system", "content": system}] + history + [
            {"role": "user", "content": q}]
        try:
            resp, sav = client.chat(messages=messages, model=model,
                                    session_id="brevitas-chat", max_tokens=400)
            answer = resp.choices[0].message.content
        except Exception as e:
            printer(f"  [error: {e}]")
            continue

        extend_bounded_list(
            history,
            [{"role": "user", "content": q}, {"role": "assistant", "content": answer}],
            max_items=bounds.demo_history_max_items,
            max_bytes=bounds.demo_history_max_bytes,
        )
        cum_uncached += sav.uncached_cost
        cum_actual += sav.actual_cost
        cum_cached += sav.cached_tokens

        printer(f"\nbot › {answer}\n")
        printer(f"  [turn {turn}] cached {sav.cached_tokens:,} tok · "
                f"this turn {sav.savings_pct:.0f}% saved · "
                f"strategy={sav.cache_placement.get('strategy', '?')}")
        if cum_uncached > 0:
            total_saved = round(100 * (1 - cum_actual / cum_uncached), 1)
            printer(f"  [running] {total_saved}% input-cost saved across {turn} turns "
                    f"({int(cum_cached):,} cached tokens so far)\n")

    printer("session ended.")


def run_demo(doc_path: str, questions: List[str], provider: str = "deepseek",
             model: str = "", api_key: str = "", printer=print) -> dict:
    """Non-interactive: ask a fixed list of questions about a doc; report per-turn + total
    token savings. Used to demonstrate the caching effect of a pinned document."""
    from brevitas import BrevitasClient
    from token_efficiency_model.lossless.provider_cache import count_tokens

    bounds = ResourceBounds.from_env()
    document = read_document(doc_path, max_bytes=bounds.demo_document_max_bytes)
    doc_tokens = count_tokens(document)
    key = _load_key(provider, api_key)
    if not key:
        raise SystemExit(f"No API key for {provider}.")
    model = model or {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}.get(provider, "gpt-4o-mini")

    client = BrevitasClient(provider=provider, api_key=key, base_url=_base_url(provider))
    system = ("Answer using the document below as the source of truth.\n\n=== DOCUMENT ===\n"
              + document + "\n=== END DOCUMENT ===")
    try:
        return _run_fixed_demo(
            client, doc_path, questions, provider, model, system, doc_tokens,
            bounds, printer,
        )
    finally:
        safe_close_resource(client)


def _run_fixed_demo(client, doc_path: str, questions: List[str], provider: str,
                    model: str, system: str, doc_tokens: int,
                    bounds: ResourceBounds, printer) -> dict:
    printer(f"Document '{os.path.basename(doc_path)}': ~{doc_tokens:,} tokens, re-sent each turn.")
    printer(f"Provider {provider}/{model}\n")

    history: List[dict] = []
    cum_uncached = cum_actual = 0.0
    cached_total = 0
    for i, q in enumerate(questions, 1):
        require_size(q, bounds.session_max_item_bytes, name="question", sizer=utf8_size)
        messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": q}]
        resp, sav = client.chat(messages=messages, model=model, session_id="demo", max_tokens=120)
        ans = resp.choices[0].message.content
        extend_bounded_list(
            history,
            [{"role": "user", "content": q}, {"role": "assistant", "content": ans}],
            max_items=bounds.demo_history_max_items,
            max_bytes=bounds.demo_history_max_bytes,
        )
        cum_uncached += sav.uncached_cost
        cum_actual += sav.actual_cost
        cached_total += sav.cached_tokens
        printer(f"[turn {i}] Q: {q[:48]}")
        printer(f"         cached={sav.cached_tokens:,} tok · this-turn saved {sav.savings_pct:.0f}%")
    total = round(100 * (1 - cum_actual / cum_uncached), 1) if cum_uncached else 0.0
    printer(f"\n=== {len(questions)} turns: {total}% total input-cost saved · "
            f"{cached_total:,} tokens served from cache ===")
    return {"turns": len(questions), "total_saved_pct": total, "cached_tokens": cached_total}
