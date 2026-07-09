"""Task-aware compression router — pick how hard to compress a prompt, by task.

Grounded in real, open-source prompt-compression research:
  * LLMLingua-2 (Pan et al., ACL'24, arXiv:2403.12968): per-prompt dynamic compression `rate`
    and `force_tokens` to PROTECT content that must survive. This module sets the rate per task
    and protects code/identifiers/numbers so we "retain as much context as possible".
  * LongLLMLingua (Jiang et al., ICLR'24, arXiv:2310.06839): task/question-aware compression —
    compress the parts least relevant to the task harder. We approximate this by protecting the
    fenced code / structured spans and only compressing the prose.

What's the real algorithm vs. the thin glue:
  - REAL (paper + library): the compression itself is LLMLingua-2 via the `llmlingua` package.
  - GLUE (transparent heuristic, configurable): the task classifier and the task->rate table.
    Aggressive on tasks that tolerate it (creative copy, boilerplate), light on precise tasks
    (math/extraction/legal). This is a routing policy, not a learned model — by design.

Fail-safe: if the `[promptopt]` extra (llmlingua) isn't installed, every task falls back to the
lossless normalization pass (never crashes, never lossy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import semantic_gate
from .prompt_optimizer import PromptOptimization, normalize_prompt, _get_llmlingua
from .provider_cache import count_tokens

_FENCE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)


# --------------------------------------------------------------------------- #
# Task classification (transparent heuristic) + per-task compression policy
# --------------------------------------------------------------------------- #
# rate = fraction of tokens to KEEP. Lower = more compression. 1.0 = lossless only.
# Values chosen conservatively to "retain as much context as possible" while still saving on
# the tasks that tolerate it. Tune via TaskCompressionRouter(rates=...).
_DEFAULT_RATES: Dict[str, float] = {
    "creative":   0.45,   # marketing reel/copy/social — verbose briefs compress well
    "code":       0.65,   # frontend/code gen — compress prose, but PROTECT code/identifiers
    "general":    0.6,    # open-ended generation
    "summarize":  0.5,    # summarization tolerates input compression
    "reasoning":  0.85,   # math/logic/planning — light touch; details matter
    "extraction": 0.9,    # exact extraction/QA — minimal; every token may be load-bearing
}

_TASK_PATTERNS = [
    ("code",       r"\b(code|function|component|react|frontend|backend|api|css|html|"
                   r"javascript|typescript|python|build (me )?a (web|app|site)|implement|refactor)\b"),
    ("creative",   r"\b(marketing|reel|ad|caption|tagline|social( media)?|tweet|post|"
                   r"slogan|brand|story|poem|script|video|instagram|tiktok|copywrit)\b"),
    ("summarize",  r"\b(summar(y|ize|ise)|tl;?dr|condense|key points|brief)\b"),
    ("extraction", r"\b(extract|find the|what is the|list all|exact|verbatim|table of|"
                   r"pull out|return the)\b"),
    ("reasoning",  r"\b(calculate|compute|prove|solve|reason|step by step|logic|math|"
                   r"derive|why|analy(s|z)e)\b"),
]


def classify_task(prompt: str, hint: Optional[str] = None) -> str:
    """Return a task class for the prompt. `hint` (an explicit task tag) wins if given."""
    if hint and hint.lower() in _DEFAULT_RATES:
        return hint.lower()
    low = (prompt or "").lower()
    for name, pat in _TASK_PATTERNS:
        if re.search(pat, low):
            return name
    return "general"


# Tasks where every token can be load-bearing, so we force-keep identifiers/numbers.
# For the rest (creative/general/summarize) heavy protection just defeats the target
# rate — those keep only structural punctuation so the achieved cut approaches 1-rate.
_HEAVY_PROTECT_TASKS = {"code", "extraction", "reasoning"}


_BASE_FORCE = ["\n", ".", "!", "?", ",", ":", ";", "(", ")", "{", "}", "[", "]", "=", "-"]


def _protect_tokens(prompt: str, task: str = "code") -> List[str]:
    """Tokens LLMLingua-2 must never drop: structural punctuation always; load-bearing NUMBERS
    additionally for precise tasks (code/extraction/reasoning).

    We deliberately do NOT stuff prose identifiers into force_tokens: LLMLingua-2 asserts on
    large/odd force lists and raises, which would crash the whole compression into a silent
    no-op. Code identifiers are already protected another way (fenced code is never compressed),
    so numbers — dates, amounts, notice periods — are the thing worth forcing."""
    if task not in _HEAVY_PROTECT_TASKS:
        return list(_BASE_FORCE)
    nums = list(dict.fromkeys(re.findall(r"\b\d[\d.,]*\b", prompt)))   # ordered-unique
    return _BASE_FORCE + nums[:50]


@dataclass
class TaskCompressionResult:
    task: str
    rate: float
    optimization: PromptOptimization
    protected_code_blocks: int
    reason: str = ""   # compressed | too_short | gate_rejected | lossless_fallback
    quality_sim: Optional[float] = None   # min accepted cosine sim across segments (None if unmeasured)


# Prose segments below this token count aren't worth compressing (and short spans distort
# badly under LLMLingua). Lowered from 40 so short-but-real prompts still get a cut.
_MIN_SEGMENT_TOKENS = 15

# A prose-segment compressor: (text, rate, force_tokens) -> compressed text (or None to skip).
CompressFn = Callable[[str, float, List[str]], Optional[str]]


@dataclass
class TaskCompressionRouter:
    """Classify a prompt's task and compress it at a task-appropriate rate (LLMLingua-2),
    protecting code blocks and key tokens. Fail-safe to lossless when llmlingua is absent.

    `compress_fn` injects the actual per-segment compressor. When None (default) the local
    LLMLingua-2 model is used; callers without torch (e.g. the API) pass a function that offloads
    to the remote compress microservice — either way the task classification, rate table, code
    protection and reassembly below stay in this one place."""

    rates: Dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_RATES))
    protect_code: bool = True
    compress_fn: Optional[CompressFn] = None

    def route(self, prompt: str, task_hint: Optional[str] = None,
              rate_override: Optional[float] = None) -> TaskCompressionResult:
        task = classify_task(prompt, task_hint)
        # rate_override lets the caller (e.g. the adaptive quality search) compress at a specific
        # keep-ratio instead of the task default, without re-implementing segment handling.
        rate = rate_override if rate_override is not None else self.rates.get(task, 0.6)

        # split out fenced code; for code tasks we never compress the code itself.
        segments = _FENCE.split(prompt)
        code_blocks = sum(1 for i, _ in enumerate(segments) if i % 2 == 1)

        # Resolve the segment compressor: injected fn wins; else the local LLMLingua model.
        compress = self.compress_fn
        if compress is None:
            comp = _get_llmlingua()
            if comp is not None:
                def compress(text, r, force, _c=comp):
                    try:
                        return _c.compress_prompt(text, rate=r, force_tokens=force).get(
                            "compressed_prompt")
                    except Exception:
                        return None

        if compress is None or rate >= 1.0:
            opt = self._lossless(prompt)
            reason = "lossless_fallback"
            if compress is None:
                opt.note = (opt.note + " " if opt.note else "") + \
                    "LLMLingua-2 unavailable (install brevitas-systems[promptopt]); lossless only."
            return TaskCompressionResult(task, rate, opt, code_blocks, reason=reason)

        before = count_tokens(prompt)
        force = _protect_tokens(prompt, task)
        # Same semantic floor the message optimizer uses: after LLMLingua drops tokens we verify
        # the compressed segment still MEANS the original (cosine >= floor) and reject it otherwise.
        # Fail-open: if the gate is disabled or similarity can't be measured, we accept — behaviour
        # is then identical to having no gate.
        gate_on = semantic_gate.gate_enabled()
        floor = semantic_gate.min_similarity()
        out_parts: List[str] = []
        compressed_any = False
        gate_rejects = 0
        worst_sim: Optional[float] = None
        for i, seg in enumerate(segments):
            if i % 2 == 1:                       # code fence — never compressed
                out_parts.append(seg)
                continue
            seg_norm = normalize_prompt(seg)
            if count_tokens(seg_norm) < _MIN_SEGMENT_TOKENS:   # too short to compress meaningfully
                out_parts.append(seg_norm)
                continue
            out = compress(seg_norm, rate, force)
            if not out and force:
                # A bad/oversized force token can make the compressor raise (-> None). Don't let
                # that silently no-op the whole segment: retry once with no force list.
                out = compress(seg_norm, rate, [])
            if out:
                sim = semantic_gate.semantic_similarity(seg_norm, out) if gate_on else None
                if sim is not None and sim < floor:
                    out_parts.append(seg_norm)   # meaning drifted too far — keep the lossless original
                    gate_rejects += 1
                    continue
                if sim is not None:
                    worst_sim = sim if worst_sim is None else min(worst_sim, sim)
                out_parts.append(out)
                compressed_any = True
            else:
                out_parts.append(seg_norm)
        optimized = "".join(out_parts).strip()
        after = count_tokens(optimized)
        reason = "compressed" if compressed_any else ("gate_rejected" if gate_rejects else "too_short")
        opt = PromptOptimization(
            original=prompt, optimized=optimized, tokens_before=before, tokens_after=after,
            saved_pct=round(100 * (1 - after / max(1, before)), 2),
            method="llmlingua2+lossless" if compressed_any else "lossless",
            lossy=compressed_any,
            note=f"task={task}, rate={rate}; code blocks protected={code_blocks}; "
                 f"gate rejects={gate_rejects} (floor={floor}). "
                 "LLMLingua-2 (arXiv:2403.12968) — lossy; verify critical prompts.",
        )
        return TaskCompressionResult(task, rate, opt, code_blocks, reason=reason,
                                     quality_sim=worst_sim)

    def _lossless(self, prompt: str) -> PromptOptimization:
        before = count_tokens(prompt)
        norm = normalize_prompt(prompt)
        after = count_tokens(norm)
        return PromptOptimization(
            original=prompt, optimized=norm, tokens_before=before, tokens_after=after,
            saved_pct=round(100 * (1 - after / max(1, before)), 2),
            method="lossless", lossy=False,
        )
