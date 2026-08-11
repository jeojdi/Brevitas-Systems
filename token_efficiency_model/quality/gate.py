"""Billing-grade quality gate: embedding similarity + position-swapped LLM judge.

Fixes over the earlier prototype gate (see CODEBASE_WEAKNESSES W6.5, brief b4):
  * judge runs at temperature 0 (a judge must be deterministic, not creative);
  * position-swap debiasing: judged twice with answer order swapped, scores
    averaged — LLM judges exhibit position bias (LLM-judge validity literature);
  * generous max_tokens so the JSON verdict is never truncated;
  * calibrator hook: the embedding/judge combination is a pluggable callable so the
    b5 isotonic calibration can replace the default without touching this module;
  * degraded assessments (judge unavailable) NEVER pass — unverified ⇒ unbilled.

The judge backend is any OpenAI-compatible endpoint, but it is EXPLICIT-OPT-IN ONLY
(BREVITAS_JUDGE_BACKEND) because running it egresses customer-derived text to a third
party — see _load_key. Nothing here imports archived legacy code.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── Lever gate (P0.6 + follow-ups) ────────────────────────────────────────────
# The single gate the request paths consult before applying an optimization. Two classes:
#
#   RISKY levers (retrieval, compression, reorder, semantic_cache) can change an answer.
#   They are FAIL-CLOSED: DENIED by default and only allowed when the operator has
#   explicitly opted in (per-lever env flag or BREVITAS_APPROVED_LEVERS) AND the lever has
#   not tripped. "Not tripped yet" is NOT enough to allow — absence of a quality signal
#   means deny, not allow.
#
#   SAFE levers (cache = the exact-hash, byte-identical response cache) are byte-preserving,
#   so they are ALLOWED by default but can still be disabled by a trip.
#
# Trips are PER-TENANT: a trip is keyed by (tenant_key, lever). One customer's failing
# quality stream disables levers only for THAT customer — never globally. A trip with the
# empty key, or BREVITAS_TRIPPED_LEVERS, is a global operator kill switch that applies to all.
# Unknown lever names always deny.

# risky lever -> the env flag that opts it in (each defaults off)
_RISKY_LEVERS = {
    "retrieval": "BREVITAS_RETRIEVAL_ENABLED",
    "compression": "BREVITAS_COMPRESS_LOSSY",
    "reorder": "BREVITAS_MESSAGE_REORDER",
    "semantic_cache": "BREVITAS_SEMANTIC_CACHE",
}
# cache = exact-hash byte-identical response cache; cache_injection = Brevitas-owned
# provider cache_control breakpoint writes (metadata-only, response bytes untouched)
_SAFE_LEVERS = {"cache", "cache_injection"}
_LEVERS = set(_RISKY_LEVERS) | _SAFE_LEVERS

_tripped_levers: set[tuple[str, str]] = set()   # (tenant_key, lever); "" key == global
_TRUTHY = {"1", "true", "yes", "on"}


def _env_tripped_levers() -> set[str]:
    raw = os.environ.get("BREVITAS_TRIPPED_LEVERS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _env_approved_levers() -> set[str]:
    raw = os.environ.get("BREVITAS_APPROVED_LEVERS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _lever_opted_in(name: str) -> bool:
    """A risky lever is opted in only by explicit operator config: its own enable flag,
    or an entry in BREVITAS_APPROVED_LEVERS."""
    env = _RISKY_LEVERS.get(name)
    if env and os.environ.get(env, "").strip().lower() in _TRUTHY:
        return True
    return name in _env_approved_levers()


def trip_lever(lever: str, key: str = "") -> None:
    """Sticky-disable a lever for tenant `key` (empty key = global). Persists for the
    process lifetime; clear via reset_lever / reset_all_levers after investigation."""
    if lever:
        _tripped_levers.add(((key or ""), lever.strip().lower()))


def reset_lever(lever: str, key: str = "") -> None:
    _tripped_levers.discard(((key or ""), (lever or "").strip().lower()))


def reset_all_levers(key: str = "") -> None:
    """Clear every lever trip for tenant `key` (used by the per-customer reset endpoint)."""
    k = key or ""
    for entry in [e for e in _tripped_levers if e[0] == k]:
        _tripped_levers.discard(entry)


def _is_tripped(name: str, key: str) -> bool:
    return (((key or ""), name) in _tripped_levers        # this tenant
            or ("", name) in _tripped_levers              # global trip
            or name in _env_tripped_levers())             # operator env kill switch


def lever_allowed(lever: str, key: str = "") -> bool:
    """True only when `lever` is safe to apply for tenant `key`. FAILS CLOSED:
      * unknown lever name          -> deny
      * any error                   -> deny
      * tripped (tenant/global/env) -> deny
      * risky lever not opted in    -> deny (absence of approval is denial, not allowance)
    Safe (byte-preserving) levers default allow; risky levers require explicit opt-in."""
    try:
        name = (lever or "").strip().lower()
        if name not in _LEVERS:
            return False
        if _is_tripped(name, key):
            return False
        if name in _SAFE_LEVERS:
            return True
        return _lever_opted_in(name)
    except Exception:
        return False


# ── Judge backend selection (fail-closed egress control) ─────────────────────
# Running the judge ships CUSTOMER-DERIVED TEXT — their question and both answers — to a
# third-party API. Picking a backend is therefore a SUBPROCESSOR decision, not a
# convenience default, so it is EXPLICIT-OPT-IN ONLY: BREVITAS_JUDGE_BACKEND must name it.
#
# What this replaces: the previous _load_key walked a hardcoded preference list and
# scraped every KEY=value out of the repo's .env.local into its lookup dict. Merely
# *having* a DeepSeek key on the box — a probe key, a benchmark key, anything unrelated —
# silently routed customer text to DeepSeek that nobody opted into. This repo's own
# .env.local carries both DEEPSEEK_API_KEY and OPENAI_API_KEY, so that was live, not
# theoretical (see docs/AI_NATIVE_REDIS_BRIEF_REVIEW.md part 12).
#
# Unset now means NO JUDGE, which is a safe state, not a broken one: assess() already
# treats a missing judge exactly like a judge outage — it falls back to the local,
# in-process embedding similarity (_embedding_similarity, no network), reports it for
# observability, and marks the assessment degraded=True / passed=False. Unverified ⇒
# unbilled is preserved; nothing here can widen what is billable.
#
# backend name -> (credential env vars in precedence order, base_url, model)
_JUDGE_BACKENDS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "deepseek": (("Deepseek_api_key", "DEEPSEEK_API_KEY"),
                 "https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": (("OPENAI_API_KEY",), "https://api.openai.com/v1", "gpt-4o-mini"),
}

# Module-level so tests can redirect it; only ever read once a backend is named (below).
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env.local"


def _judge_backend_name(explicit: Optional[str] = None) -> str:
    """The opted-in backend name, normalized. Empty string == no judge (the default)."""
    raw = explicit if explicit is not None else os.environ.get("BREVITAS_JUDGE_BACKEND", "")
    return (raw or "").strip().lower()


def _env_file_credential(names: tuple[str, ...]) -> str:
    """First value in .env.local matching one of `names`, in `names` order.

    Deliberately narrow: only the *named backend's own* credential names are looked up,
    never the whole file into a dict, so an unrelated provider's key sitting in that file
    can never become the judge's egress target. Any read/parse failure is a miss — a judge
    we cannot configure must fail closed, not raise out of QualityGate.__init__.
    """
    try:
        if not _ENV_FILE.exists():
            return ""
        found: dict[str, str] = {}
        for line in _ENV_FILE.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            if k in names and k not in found:      # first occurrence wins, as before
                found[k] = v.strip()
        for n in names:
            if found.get(n):
                return found[n]
        return ""
    except Exception as e:
        logger.warning(f"quality gate: could not read {_ENV_FILE.name} for the judge "
                       f"credential: {e}")
        return ""


def _load_key(backend: Optional[str] = None) -> tuple[str, str, str]:
    """Return (key, base_url, model) for the EXPLICITLY opted-in judge backend, or the
    empty triple — which callers read as "no judge" — in every other case.

    FAILS CLOSED, and never substitutes a provider for the one that was named:
      * BREVITAS_JUDGE_BACKEND unset/blank -> ("", "", "") and .env.local is NEVER read
      * unknown backend name (a typo)      -> ("", "", ""); a typo must not fall through
                                              to some other subprocessor
      * named backend's credential missing -> ("", "", ""); never borrow a different
                                              provider's key to satisfy the request
    Process env wins over .env.local, as it did before.
    """
    name = _judge_backend_name(backend)
    if not name:
        return "", "", ""                          # no opt-in ⇒ no judge, no file read
    spec = _JUDGE_BACKENDS.get(name)
    if spec is None:
        logger.error("quality gate: BREVITAS_JUDGE_BACKEND=%r is not a known judge backend "
                     "(%s) — judge disabled", name, "/".join(sorted(_JUDGE_BACKENDS)))
        return "", "", ""
    cred_names, base_url, model = spec
    key = next((os.environ[n] for n in cred_names if os.environ.get(n)), "")
    if not key:
        key = _env_file_credential(cred_names)
    if not key:
        logger.error("quality gate: judge backend %r opted in but none of %s is set — "
                     "judge disabled (no fallback to another provider)",
                     name, "/".join(cred_names))
        return "", "", ""
    return key, base_url, model


@dataclass
class QualityAssessment:
    score: float                    # calibrated/combined 0-1 retention score
    passed: bool                    # score >= floor AND not degraded
    embedding_similarity: float
    judge_score: Optional[float]    # None when the judge never ran
    judge_reasoning: Optional[str] = None
    degraded: bool = False          # judge unavailable ⇒ can never pass
    fallback_reason: Optional[str] = None


@dataclass
class QualityGateConfig:
    floor: float = 0.8
    judge_temperature: float = 0.0        # determinism (b4)
    judge_max_tokens: int = 300           # never truncate the verdict JSON
    position_swap: bool = True            # two calls, order swapped, averaged
    timeout: int = 20
    embedding_model: str = "all-MiniLM-L6-v2"
    # combiner(embedding_sim, judge_score) -> combined score. Default is a simple
    # mean; brief b5 replaces this with an isotonic-calibrated combiner.
    combiner: Optional[Callable[[float, float], float]] = None
    # b5 calibration: a fitted quality.calibration.Calibrator mapping the raw combined
    # score to empirical P(correct). When set, the floor is a TARGET RISK
    # (pass iff P(correct) >= floor) instead of a raw-score threshold — raw 0.8 cosine
    # means different things per model/task family. None/unfitted ⇒ raw-score behavior.
    calibrator: Optional[object] = None


_JUDGE_PROMPT = """You are a strict semantic-equivalence judge. Compare the two answers
to the question and rate whether ANSWER_B preserves the meaning, factual content and
completeness of ANSWER_A.

Question: {question}

ANSWER_A:
{a}

ANSWER_B:
{b}

Score 0.0-1.0 (1.0 = fully equivalent; 0.0 = contradictory or unrelated).
Respond ONLY with one JSON object: {{"score": <number>, "reasoning": "<short>"}}"""


class QualityGate:
    """Assess whether an optimized answer preserves the reference answer's quality."""

    def __init__(self, config: Optional[QualityGateConfig] = None):
        self.config = config or QualityGateConfig()
        self._model = None
        self._model_tried = False
        self.judge_key, self.judge_base, self.judge_model = _load_key()
        if not self.judge_key:
            # Expected whenever nobody opted a backend in — the fail-closed default. Still
            # logged at error level: anyone who constructed a gate expecting verdicts is
            # about to get assessments that can never pass, and should know why.
            logger.error("quality gate: no judge backend configured (set "
                         "BREVITAS_JUDGE_BACKEND to one of %s plus that backend's API key) "
                         "— assessments will be degraded (embedding-only) and can never pass",
                         "/".join(sorted(_JUDGE_BACKENDS)))

    # ------------------------------------------------------------------ public
    def assess(self, optimized_answer: str, reference_answer: str,
               question: str) -> QualityAssessment:
        emb = self._embedding_similarity(optimized_answer, reference_answer)
        judge, reasoning, why = self._judge(optimized_answer, reference_answer, question)

        if judge is None:
            # degraded: never passes; embedding reported for observability only
            return QualityAssessment(score=min(emb, 0.0 + emb * 0.9), passed=False,
                                     embedding_similarity=emb, judge_score=None,
                                     degraded=True, fallback_reason=why)

        combine = self.config.combiner or (lambda e, j: 0.5 * e + 0.5 * j)
        score = max(0.0, min(1.0, combine(emb, judge)))
        cal = self.config.calibrator
        if cal is not None and getattr(cal, "fitted", False):
            score = max(0.0, min(1.0, float(cal.p_correct(score))))
        return QualityAssessment(score=score, passed=score >= self.config.floor,
                                 embedding_similarity=emb, judge_score=judge,
                                 judge_reasoning=reasoning)

    # ------------------------------------------------------------- embeddings
    def _encoder(self):
        if self._model_tried:
            return self._model
        self._model_tried = True
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.config.embedding_model)
        except Exception as e:
            logger.error(f"quality gate: embedding model unavailable: {e}")
            self._model = None
        return self._model

    def _embedding_similarity(self, a: str, b: str) -> float:
        m = self._encoder()
        if m is None or not a.strip() or not b.strip():
            return 0.0
        try:
            from sentence_transformers import util
            e = m.encode([a, b], convert_to_tensor=True, show_progress_bar=False)
            return max(0.0, min(1.0, float(util.cos_sim(e[0], e[1]).item())))
        except Exception as e:
            logger.error(f"quality gate: embedding similarity failed: {e}")
            return 0.0

    # ------------------------------------------------------------------ judge
    def _judge(self, optimized: str, reference: str, question: str):
        """Position-swapped deterministic judge. Returns (score|None, reasoning, why)."""
        if not self.judge_key:
            # No opt-in (or the opted-in credential is missing): do not call anyone. The
            # caller degrades to the local embedding path; degraded can never pass.
            return None, None, "no judge backend configured (BREVITAS_JUDGE_BACKEND)"
        s1 = self._judge_once(reference, optimized, question)   # A=ref, B=opt
        if s1 is None:
            return None, None, "judge call failed"
        if not self.config.position_swap:
            return s1[0], s1[1], None
        s2 = self._judge_once(optimized, reference, question)   # swapped order
        if s2 is None:
            # one good sample beats zero; flag the missing swap in reasoning
            return s1[0], (s1[1] or "") + " [swap call failed]", None
        return (s1[0] + s2[0]) / 2.0, s1[1], None

    def _judge_once(self, a: str, b: str, question: str):
        try:
            import httpx
            r = httpx.post(
                f"{self.judge_base}/chat/completions",
                headers={"Authorization": f"Bearer {self.judge_key}"},
                json={"model": self.judge_model,
                      "temperature": self.config.judge_temperature,
                      "max_tokens": self.config.judge_max_tokens,
                      "messages": [{"role": "user", "content": _JUDGE_PROMPT.format(
                          question=question[:2000], a=a[:4000], b=b[:4000])}]},
                timeout=self.config.timeout,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            start, end = text.find("{"), text.rfind("}") + 1
            data = json.loads(text[start:end])
            return (max(0.0, min(1.0, float(data["score"]))),
                    str(data.get("reasoning", ""))[:400])
        except Exception as e:
            logger.warning(f"quality gate: judge call failed: {e}")
            return None
