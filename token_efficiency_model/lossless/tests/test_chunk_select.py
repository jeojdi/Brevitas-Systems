"""Chunk-level retrieval helpers (api_adapter) + surviving router invariants.
Salvaged from the superseded test_router_unified.py when the unified compress-lever
router was replaced by the verified dollar router (b0)."""

import numpy as np
import pytest

from token_efficiency_model.lossless import api_adapter
from token_efficiency_model.lossless.api_adapter import chunk_text, select_chunk_indices
from token_efficiency_model.lossless.router import BrevitasRouter

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
    import time; monkeypatch.setattr(api_adapter, "_ENCODER_LAST_TRIED", time.time())
    yield


def test_chunk_text_splits_and_preserves_content():
    text = "\n\n".join(f"Paragraph {i} about topic number {i}." for i in range(50))
    chunks = chunk_text(text, target_tokens=64)
    assert len(chunks) > 1
    joined = " ".join(chunks)
    for i in (0, 25, 49):
        assert f"topic number {i}." in joined   # nothing dropped


def test_select_chunk_indices_picks_relevant(fake_encoder):
    chunks = ["binary search log time", "photosynthesis chloroplast energy",
              "french revolution bastille", "mitochondria atp energy"]
    out = select_chunk_indices("binary search log", chunks, k=1)
    assert not out["fallback_applied"]
    assert out["indices"] == [0]


def test_select_chunk_indices_fallback_when_no_encoder(monkeypatch):
    monkeypatch.setattr(api_adapter, "_ENCODER", None)
    import time; monkeypatch.setattr(api_adapter, "_ENCODER_LAST_TRIED", time.time())
    out = select_chunk_indices("anything", ["a", "b", "c"], k=2)
    assert out["fallback_applied"] and out["indices"] == [0, 1, 2]


def test_small_repeating_context_routes_to_cache():
    r = BrevitasRouter(provider="deepseek")
    ctx = ["word " * 1500]
    r.decide("s", ctx, "q1")
    d = r.decide("s", ctx, "q2")   # context now repeats -> caching wins
    assert d.strategy == "cache_only"


def test_below_min_is_passthrough():
    r = BrevitasRouter(provider="openai")
    assert r.decide("s", ["tiny ctx"], "hi").strategy == "passthrough"


def test_deepseek_discount_is_real_rate_not_90pct():
    from token_efficiency_model.lossless.router import CACHE_DISCOUNT
    assert abs(CACHE_DISCOUNT["deepseek"] - 0.02) < 1e-6
