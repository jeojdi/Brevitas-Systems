"""Single-prompt token optimization — shrink ONE prompt's token count.

This is a different lever from caching/dedup/delta (which avoid re-sending repeated context).
Here we reduce the tokens of a single, one-shot prompt. Two layers:

1. LOSSLESS (rate >= 1.0): returns the prompt BYTE-IDENTICAL. Whitespace normalization is
   NOT applied here — collapsing spaces/tabs corrupts indentation-significant content
   (YAML, Python, Makefiles, Markdown), so it is only ever a pre-step for the lossy path.

2. LLMLingua-2 compression (opt-in, LOSSY, paper-backed): faithful prompt compression via the
   published `llmlingua` library — LLMLingua-2 (Pan et al., ACL'24, arXiv:2403.12968), a token
   classifier (keep/discard) distilled from GPT-4. Gives 2-5x compression with strong
   answer-retention, but it DOES drop tokens, so it is lossy and opt-in via `rate < 1.0`.
   Requires the `[promptopt]` extra (`pip install brevitas-systems[promptopt]`); if unavailable,
   we fail safe to lossless-only and say so.

Token counts are measured with the real tokenizer (tiktoken cl100k_base).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .provider_cache import count_tokens

_FENCE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)


@dataclass
class PromptOptimization:
    original: str
    optimized: str
    tokens_before: int
    tokens_after: int
    saved_pct: float
    method: str          # "lossless" | "llmlingua2" | "llmlingua2+lossless"
    lossy: bool
    note: str = ""


def normalize_prompt(text: str) -> str:
    """Lossless whitespace/format normalization. Code fences are left byte-identical so
    indentation-significant content (Python, YAML, etc.) is never altered."""
    if not text:
        return text
    parts = _FENCE.split(text)
    out = []
    for i, seg in enumerate(parts):
        if i % 2 == 1:                      # odd indices are the captured code fences
            out.append(seg)
            continue
        # prose segment: safe, meaning-preserving cleanups
        seg = re.sub(r"[ \t]+", " ", seg)          # runs of spaces/tabs -> single space
        seg = re.sub(r" *\n", "\n", seg)           # trailing spaces before newlines
        seg = re.sub(r"\n{3,}", "\n\n", seg)        # 3+ blank lines -> one blank line
        out.append(seg)
    return "".join(out).strip()


# --- optional LLMLingua-2 (lazy, fail-safe) -------------------------------- #
_LLMLINGUA = None
_LLMLINGUA_TRIED = False


def _get_llmlingua():
    global _LLMLINGUA, _LLMLINGUA_TRIED
    if _LLMLINGUA_TRIED:
        return _LLMLINGUA
    _LLMLINGUA_TRIED = True
    try:
        from llmlingua import PromptCompressor
        # Detect device so CPU-only hosts don't crash (PromptCompressor defaults to "cuda").
        try:
            import torch
            _device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            _device = "cpu"
        # bert-base-multilingual: lighter/faster and loads reliably on small containers;
        # kept identical to services/compress/app.py so both paths behave the same.
        _LLMLINGUA = PromptCompressor(
            model_name="microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank",
            use_llmlingua2=True,
            device_map=_device,
        )
    except Exception:
        _LLMLINGUA = None
    return _LLMLINGUA


def optimize_prompt(text: str, rate: float = 1.0,
                    force_tokens: Optional[list] = None) -> PromptOptimization:
    """Optimize a single prompt's token count.

    Args:
        text: the prompt to shrink.
        rate: target keep-ratio. 1.0 = lossless normalization only (safe, default). A value
            < 1.0 (e.g. 0.5 keeps ~half) enables LLMLingua-2 compression (LOSSY). Requires the
            `[promptopt]` extra; falls back to remote service if configured, then to lossless-only.
        force_tokens: tokens LLMLingua-2 must never drop (e.g. ["\n", ".", "?"]).

    Returns a PromptOptimization with before/after token counts measured by tiktoken.
    """
    before = count_tokens(text)

    # Lossless MUST be byte-identical. normalize_prompt collapses whitespace outside code
    # fences and corrupts indentation-significant content (YAML, Python, Makefiles,
    # Markdown), so it is NEVER applied on a lossless return — only as a lossy-path pre-step.
    if rate >= 1.0:
        return PromptOptimization(
            original=text, optimized=text, tokens_before=before, tokens_after=before,
            saved_pct=0.0, method="lossless", lossy=False,
        )

    # rate < 1.0 -> LLMLingua-2 (lossy, opt-in). Normalization is allowed here.
    normalized = normalize_prompt(text)
    comp = _get_llmlingua()
    if comp is not None:
        try:
            result = comp.compress_prompt(
                normalized, rate=rate,
                force_tokens=force_tokens or ["\n", ".", "!", "?", ",", ":"],
            )
            compressed = result.get("compressed_prompt", normalized)
            after = count_tokens(compressed)
            return PromptOptimization(
                original=text, optimized=compressed, tokens_before=before, tokens_after=after,
                saved_pct=round(100 * (1 - after / max(1, before)), 2),
                method="llmlingua2+lossless", lossy=True,
                note="LLMLingua-2 (arXiv:2403.12968) — lossy compression; verify output on critical prompts.",
            )
        except Exception as e:
            pass  # Fall through to remote/lossless

    # Local LLMLingua unavailable or failed; try remote service next
    from . import remote_compress
    if remote_compress.remote_available():
        remote_result = remote_compress.remote_optimize(
            normalized, rate=rate, force_tokens=force_tokens
        )
        if remote_result is not None:
            return remote_result

    # All compressions unavailable or failed; fall back to lossless (byte-identical original).
    note = "LLMLingua-2 unavailable (install brevitas-systems[promptopt]); used lossless only."
    if remote_compress.remote_available():
        note = "Remote and local LLMLingua-2 unavailable; used lossless only."

    return PromptOptimization(
        original=text, optimized=text, tokens_before=before, tokens_after=before,
        saved_pct=0.0, method="lossless", lossy=False,
        note=note,
    )
