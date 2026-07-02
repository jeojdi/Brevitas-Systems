"""Tests for the unified router — per-request choice of cache / retrieve / compress — and
chunk-level retrieval that slices INTO a large document."""

import numpy as np
import pytest

from token_efficiency_model.lossless import api_adapter, engine
from token_efficiency_model.lossless.api_adapter import chunk_text, select_chunk_indices
from token_efficiency_model.lossless.engine import optimize_request, _msg_text
from token_efficiency_model.lossless.provider_cache import count_tokens
from token_efficiency_model.lossless.router import BrevitasRouter


# --------------------------------------------------------------------------- #
# deterministic bag-of-words encoder (no heavy model in tests)
# --------------------------------------------------------------------------- #
_VOCAB = ["binary", "search", "photosynthesis", "revolution", "mitochondria",
          "atp", "log", "chloroplast", "bastille", "energy"]


class _FakeEncoder:
    def encode(self, texts, normalize_embeddings=True):
        vecs = []
        for t in texts:
            low = t.lower()
            v = np.array([float(low.count(w)) for w in _VOCAB], dtype=np.float32)
            if normalize_embeddings:
                n = np.linalg.norm(v)
                v = v / n if n else v
            vecs.append(v)
        return np.asarray(vecs, dtype=np.float32)


@pytest.fixture
def fake_encoder(monkeypatch):
    monkeypatch.setattr(api_adapter, "_ENCODER", _FakeEncoder())
    monkeypatch.setattr(api_adapter, "_ENCODER_TRIED", True)
    yield


# --------------------------------------------------------------------------- #
# router decision: cache vs retrieve vs compress vs passthrough
# --------------------------------------------------------------------------- #
def test_big_doc_routes_to_retrieve():
    r = BrevitasRouter(provider="deepseek")
    d = r.decide("s", ["word " * 20000], "what does chapter 3 say about X?")
    assert d.strategy == "retrieve"
    assert d.costs["retrieve"] < d.costs["cache_only"]


def test_small_repeating_context_routes_to_cache():
    r = BrevitasRouter(provider="deepseek")
    ctx = ["word " * 1500]
    r.decide("s", ctx, "q1")
    d = r.decide("s", ctx, "q2")   # context now repeats -> caching wins
    assert d.strategy == "cache_only"


def test_compress_only_when_lossy_allowed():
    ctx = ["brief " * 3000]
    off = BrevitasRouter(provider="openai", allow_lossy=False)
    assert "compress" not in off.decide("s", ctx, "write a marketing reel caption").costs
    on = BrevitasRouter(provider="openai", allow_lossy=True)
    d = on.decide("s", ctx, "write a marketing reel caption for instagram")
    assert d.task == "creative"
    assert d.strategy == "compress"   # creative keep-rate (0.45) beats retrieve/cache here


def test_below_min_is_passthrough():
    r = BrevitasRouter(provider="openai")
    assert r.decide("s", ["tiny ctx"], "hi").strategy == "passthrough"


def test_deepseek_discount_is_real_rate_not_90pct():
    from token_efficiency_model.lossless.router import CACHE_DISCOUNT
    assert abs(CACHE_DISCOUNT["deepseek"] - 0.259) < 1e-6   # not 0.10


# --------------------------------------------------------------------------- #
# chunk_text
# --------------------------------------------------------------------------- #
def test_chunk_text_splits_and_preserves_content():
    text = "\n\n".join(f"Paragraph {i} about topic number {i}." for i in range(50))
    chunks = chunk_text(text, target_tokens=64)
    assert len(chunks) > 1
    joined = " ".join(chunks)
    for i in (0, 25, 49):
        assert f"topic number {i}." in joined   # nothing dropped


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


# --------------------------------------------------------------------------- #
# chunk-level selection + engine slicing into a big document
# --------------------------------------------------------------------------- #
def test_select_chunk_indices_picks_relevant(fake_encoder):
    chunks = ["binary search log time", "photosynthesis chloroplast energy",
              "french revolution bastille", "mitochondria atp energy"]
    out = select_chunk_indices("binary search log", chunks, k=1)
    assert not out["fallback_applied"]
    assert out["indices"] == [0]


def test_select_chunk_indices_fallback_when_no_encoder(monkeypatch):
    monkeypatch.setattr(api_adapter, "_ENCODER", None)
    monkeypatch.setattr(api_adapter, "_ENCODER_TRIED", True)
    out = select_chunk_indices("anything", ["a", "b", "c"], k=2)
    assert out["fallback_applied"] and out["indices"] == [0, 1, 2]


def test_engine_chunk_retrieval_slices_into_document(fake_encoder):
    # large enough (and with sentence boundaries) that there are many more chunks than k,
    # so irrelevant sections get dropped
    secs = {
        "binary search": "Binary search runs in log time. " * 600,
        "photosynthesis": "Photosynthesis happens in the chloroplast for energy. " * 600,
        "revolution": "The french revolution stormed the bastille. " * 600,
    }
    book = "\n\n".join(f"## {k}\n{v}" for k, v in secs.items())
    body = {"model": "deepseek-chat", "messages": [
        {"role": "user", "content": book},
        {"role": "user", "content": "explain binary search log time"},
    ]}
    r = BrevitasRouter(provider="deepseek")
    meta = optimize_request(body, "deepseek", r, "sess")
    assert meta["strategy"] == "retrieve" and meta["level"] == "chunk"
    assert meta["optimized_tokens"] < meta["baseline_tokens"] * 0.7   # real reduction
    kept = _msg_text(body["messages"][0]["content"]).lower()
    assert "binary search" in kept
    assert "french revolution" not in kept   # irrelevant section dropped
    # the volatile question is untouched
    assert body["messages"][-1]["content"] == "explain binary search log time"


def test_engine_falls_back_to_cache_when_retrieval_bails(monkeypatch):
    # encoder unavailable -> chunk + message retrieval both fail safe -> cache_only
    monkeypatch.setattr(api_adapter, "_ENCODER", None)
    monkeypatch.setattr(api_adapter, "_ENCODER_TRIED", True)
    body = {"model": "deepseek-chat", "messages": [
        {"role": "user", "content": "word " * 20000},
        {"role": "user", "content": "a question"},
    ]}
    r = BrevitasRouter(provider="deepseek")
    meta = optimize_request(body, "deepseek", r, "sess")
    assert meta["strategy"] == "cache_only"
    assert len(body["messages"]) == 2   # context preserved, nothing thinned
