"""Billing-grade quality gate: embedding similarity + position-swapped LLM judge.

Fixes over the earlier prototype gate (see CODEBASE_WEAKNESSES W6.5, brief b4):
  * judge runs at temperature 0 (a judge must be deterministic, not creative);
  * position-swap debiasing: judged twice with answer order swapped, scores
    averaged — LLM judges exhibit position bias (LLM-judge validity literature);
  * generous max_tokens so the JSON verdict is never truncated;
  * calibrator hook: the embedding/judge combination is a pluggable callable so the
    b5 isotonic calibration can replace the default without touching this module;
  * degraded assessments (judge unavailable) NEVER pass — unverified ⇒ unbilled.

The judge backend is any OpenAI-compatible endpoint; keys come from the local
environment. Nothing here imports archived legacy code.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _load_key(names=("Deepseek_api_key", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")) -> tuple[str, str, str]:
    """Return (key, base_url, model) for the cheapest configured judge backend."""
    env = dict(os.environ)
    envfile = Path(__file__).resolve().parents[2] / ".env.local"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    if env.get(names[0]) or env.get(names[1]):
        return (env.get(names[0]) or env.get(names[1]), "https://api.deepseek.com/v1",
                "deepseek-chat")
    if env.get("OPENAI_API_KEY"):
        return env["OPENAI_API_KEY"], "https://api.openai.com/v1", "gpt-4o-mini"
    return "", "", ""


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
            logger.error("quality gate: no judge API key configured — assessments "
                         "will be degraded (embedding-only) and can never pass")

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
            return None, None, "no judge key configured"
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
