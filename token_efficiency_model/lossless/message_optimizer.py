"""Server-side single-message optimizer for the /v1/compress path — STRUCTURE-AWARE, per-segment.

A prompt is not one blob, and neither is its Context. We parse the message into typed segments and
compress ONLY the CONTEXT (background, repeated explanation, duplicate history); the output-driving
parts (task, constraints, formatting, style, examples) pass through byte-identical.

The key to real savings is gating PER CONTEXT SEGMENT, not over the whole prompt. Each CONTEXT
block is split into sentences and every sentence is compressed independently at the most aggressive
rate that still clears its own gate — so a redundant sentence compresses hard while a dense one
stays light. A single whole-prompt cosine gate can't do this: it's dominated by the untouched
parts, so it either rejects everything or waves through nonsense. Per-sentence gating roughly
doubles context savings (measured ~23% -> ~54% on a RAG corpus) with the answer-bearing facts
preserved.

Two gates per sentence, both must hold to accept a compression:
  * information density — the sentence's numbers/entities/key terms are retained (`quality_metrics`).
    This is the hard answer-preservation guarantee.
  * a semantic floor — cosine(sentence, compressed) >= BREVITAS_QUALITY_MIN_SIM (default 0.75) to
    catch catastrophic garbling. Set it to 0 to disable.
A final GLOBAL information-density check guards the whole prompt as a backstop. Compression is
offloaded to the remote LLMLingua-2 microservice; the API image stays light. Fail-safe throughout:
anything unmeasurable or unavailable returns the original with a reason code.
"""

from __future__ import annotations

import re
from typing import List, Optional

from . import remote_compress, semantic_gate
from .prompt_structure import parse, high_value_tokens
from .provider_cache import count_tokens
from .quality_metrics import information_density
from .task_router import _BASE_FORCE

# Keep-ratios to try per sentence, most aggressive (lowest keep) first.
_CONTEXT_LADDER = [0.4, 0.55, 0.7, 0.85]


def _context_ladder() -> List[float]:
    return list(_CONTEXT_LADDER)


def _sentences(text: str) -> List[str]:
    """Split into sentence-ish chunks whose concatenation reproduces `text` exactly (each chunk
    keeps its trailing terminator + whitespace), so reassembly is lossless."""
    out: List[str] = []
    last = 0
    for m in re.finditer(r"[.!?]+[\s]+", text):
        out.append(text[last:m.end()])
        last = m.end()
    if last < len(text):
        out.append(text[last:])
    return out or [text]


def optimize_message_text(text: str) -> dict:
    """Compress a message structure-aware with per-context-segment gating. Returns a dict:
    {text, tokens_before, tokens_after, method, reason, task, rate, quality_sim, info_density, roles}
    reason ∈ compressed | quality_gate | no_context | remote_unavailable | remote_error | empty.
    On anything but `compressed`, `text` is the original (fail-safe). `rate` is the most aggressive
    keep-ratio actually applied; `quality_sim` the worst per-sentence cosine among accepted sentences
    (or the best seen when nothing was accepted)."""
    before = count_tokens(text or "")
    base = {"text": text, "tokens_before": before, "tokens_after": before, "method": "lossless",
            "task": None, "rate": None, "quality_sim": None, "info_density": None, "roles": None}

    if not text or not text.strip():
        return {**base, "reason": "empty"}
    if not remote_compress.remote_available():
        return {**base, "reason": "remote_unavailable"}

    segments = parse(text)
    roles = sorted({s.role for s in segments})
    if not any(s.compressible for s in segments):
        return {**base, "reason": "no_context", "roles": roles}

    force = _BASE_FORCE + high_value_tokens(text)
    gate_on = semantic_gate.gate_enabled()
    floor = semantic_gate.min_similarity()

    stats = {"calls": 0, "fails": 0, "output": False, "accepted_rates": [], "accepted_sims": [],
             "best_sim": None}

    def _try(unit: str, rate: float) -> Optional[str]:
        stats["calls"] += 1
        ro = remote_compress.remote_optimize(unit, rate=rate, force_tokens=force)
        if ro is None:
            stats["fails"] += 1
            return None
        return ro.optimized

    def _compress_sentence(unit: str) -> str:
        """Most aggressive rate whose compression clears BOTH gates for THIS sentence; else keep."""
        if count_tokens(unit) < 12:            # too short to compress meaningfully
            return unit
        for rate in _CONTEXT_LADDER:
            out = _try(unit, rate)
            if not out or not out.strip():
                continue
            stats["output"] = True
            sim = semantic_gate.semantic_similarity(unit, out) if gate_on else None
            if sim is not None and (stats["best_sim"] is None or sim > stats["best_sim"]):
                stats["best_sim"] = sim
            sim_ok = (not gate_on) or (sim is None) or (sim >= floor)
            if sim_ok and information_density(unit, out)["overall_ok"]:
                stats["accepted_rates"].append(rate)
                if sim is not None:
                    stats["accepted_sims"].append(sim)
                return out
        return unit

    parts: List[str] = []
    for seg in segments:
        if not seg.compressible:
            parts.append(seg.text)             # output-driving -> byte-identical
            continue
        parts.append("".join(_compress_sentence(s) for s in _sentences(seg.text)))

    candidate = "".join(parts)

    if not stats["accepted_rates"]:
        # nothing was accepted. Distinguish the causes for observability.
        if stats["output"]:
            reason = "quality_gate"            # compressions were produced but all failed the gates
        elif stats["calls"] and stats["fails"] == stats["calls"]:
            reason = "remote_error"            # every remote call failed
        else:
            reason = "no_context"              # segments too short to compress
        return {**base, "reason": reason, "roles": roles,
                "quality_sim": round(stats["best_sim"], 4) if stats["best_sim"] is not None else None}

    # Global backstop: the whole prompt must still retain its critical information.
    dens = information_density(text, candidate)
    if not dens["overall_ok"]:
        return {**base, "reason": "quality_gate", "roles": roles, "info_density": dens,
                "quality_sim": round(stats["best_sim"], 4) if stats["best_sim"] is not None else None}

    sims = stats["accepted_sims"]
    return {"text": candidate, "tokens_before": before, "tokens_after": count_tokens(candidate),
            "method": "structural+llmlingua2", "reason": "compressed", "task": None,
            "rate": min(stats["accepted_rates"]),
            "quality_sim": round(min(sims), 4) if sims else None,
            "info_density": dens, "roles": roles}
