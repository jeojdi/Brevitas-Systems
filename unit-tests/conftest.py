"""Shared fixtures for the compress-path unit tests.

CI has neither the `llmlingua` package nor a live compress microservice, so these tests inject a
DETERMINISTIC fake compressor through the new `compress_fn` seam (and monkeypatch
`remote_compress.remote_optimize`). The fake honours `force_tokens` — any forced word is never
dropped — so we can assert both the savings band and the protection contract without a model.
"""

import os
import sys
from pathlib import Path

import pytest

# Repo root on sys.path so `token_efficiency_model` / `api` import when pytest is run from anywhere.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# HF tokenizers warns/deadlocks when the process forks after use (pytest + fastembed/llmlingua).
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def load_dotenv():
    """Populate os.environ from the repo .env (without overriding already-set vars).

    Used by the real-LLM tests to pick up OPENAI_API_KEY / DEEPSEEK_API_KEY.
    """
    envf = _ROOT / ".env"
    if not envf.exists():
        return
    for line in envf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip())


@pytest.fixture
def local_llmlingua_remote(monkeypatch):
    """Point the remote compress hook at the LOCAL LLMLingua-2 model so the full structural
    pipeline (real compression + real gates) runs self-contained in tests. Skips without llmlingua."""
    from token_efficiency_model.lossless import remote_compress
    from token_efficiency_model.lossless.prompt_optimizer import (
        _get_llmlingua, normalize_prompt, PromptOptimization)
    from token_efficiency_model.lossless.provider_cache import count_tokens

    comp = _get_llmlingua()
    if comp is None:
        pytest.skip("llmlingua not installed")
    monkeypatch.setattr(remote_compress, "remote_available", lambda: True)

    def _local(text, rate, force_tokens=None, **kw):
        try:
            out = comp.compress_prompt(normalize_prompt(text), rate=rate,
                                       force_tokens=force_tokens or ["\n", ".", ",", "?"]
                                       ).get("compressed_prompt", text)
        except Exception:
            out = None
        if not out:
            return None
        return PromptOptimization(text, out, count_tokens(text), count_tokens(out),
                                  0.0, "llmlingua2", True)

    monkeypatch.setattr(remote_compress, "remote_optimize", _local)
    return comp


class RecordingCompressor:
    """A fake per-segment compressor: drops a deterministic fraction of words to approximate
    `rate`, but NEVER drops a word present in `force_tokens`. Records every call for assertions."""

    def __init__(self):
        self.calls = []  # list of (segment, rate, force_tokens)

    def __call__(self, seg: str, rate: float, force):
        self.calls.append((seg, rate, list(force or [])))
        force_set = set(force or [])
        keep_every = max(1, round(1.0 / max(0.05, 1.0 - rate)))  # drop ~ (1-rate) of words
        out = []
        for i, w in enumerate(seg.split()):
            if w in force_set:          # forced token — always survives
                out.append(w)
            elif i % keep_every != 0:   # deterministic drop pattern
                out.append(w)
        return " ".join(out)


@pytest.fixture
def recording_compressor():
    return RecordingCompressor()


@pytest.fixture
def fake_remote(monkeypatch):
    """Make `remote_compress` look configured + healthy, backed by the fake compressor.

    Yields the RecordingCompressor so tests can inspect the calls the router made.
    """
    from token_efficiency_model.lossless import remote_compress, semantic_gate
    from token_efficiency_model.lossless.prompt_optimizer import PromptOptimization
    from token_efficiency_model.lossless.provider_cache import count_tokens

    comp = RecordingCompressor()

    monkeypatch.setattr(remote_compress, "remote_available", lambda: True)
    # The fake compressor drops words non-semantically, which would trip a real embedding gate.
    # Disable the gate here so these tests isolate the compress/reason plumbing; the gate has its
    # own dedicated tests. (BREVITAS_QUALITY_MIN_SIM=0 == gate off.)
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.0)

    def _remote_optimize(text, rate, force_tokens=None, **kw):
        optimized = comp(text, rate, force_tokens)
        return PromptOptimization(
            original=text, optimized=optimized,
            tokens_before=count_tokens(text), tokens_after=count_tokens(optimized),
            saved_pct=0.0, method="llmlingua2+lossless", lossy=True,
        )

    monkeypatch.setattr(remote_compress, "remote_optimize", _remote_optimize)
    return comp


LONG_PROMPT = (
    "Please write a comprehensive and detailed marketing brief for our upcoming product "
    "launch, covering the target audience, the core messaging pillars, the tone of voice, "
    "the channels we should prioritise, and a rough timeline for the whole campaign rollout. "
) * 4
