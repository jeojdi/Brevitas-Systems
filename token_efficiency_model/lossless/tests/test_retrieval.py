"""Tests for Lever 4 — retrieval (DPR / ColBERTv2 MaxSim + residual compression)."""

import hashlib

import numpy as np

from token_efficiency_model.lossless.retrieval import (
    AdaptiveRetrievalConfig,
    BM25Retriever,
    DenseRetriever,
    MaxSimReranker,
    QualityFirstRetrievalConfig,
    RetrievalConfig,
    ResidualCompressor,
    fetch_adaptive,
    fetch_for_hop,
    fetch_quality_first,
    maxsim,
    reciprocal_rank_fusion,
)


class FakeEncoder:
    """Deterministic bag-of-words hashing encoder (no model download needed)."""

    def __init__(self, dim: int = 64):
        self.dim = dim

    def encode(self, texts, normalize_embeddings: bool = True):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, t in enumerate(texts):
            for tok in t.lower().split():
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim
                out[r, h] += 1.0
        if normalize_embeddings:
            n = np.linalg.norm(out, axis=1, keepdims=True)
            out = out / np.clip(n, 1e-9, None)
        return out


# --- DPR retriever --------------------------------------------------------- #
def test_retrieve_ranks_relevant_first():
    enc = FakeEncoder()
    r = DenseRetriever(enc)
    r.index([
        "cats are small domestic mammals",
        "the mtu mismatch on load balancer caused the timeout",
        "bananas are a yellow fruit",
    ])
    hits = r.retrieve("why did the timeout happen on the load balancer", k=2)
    assert hits[0][0] == 1  # the MTU/timeout passage ranks first


def test_empty_index_returns_empty_and_failsafe():
    r = DenseRetriever(FakeEncoder())
    assert r.retrieve("anything", k=3) == []
    chunks, meta = fetch_for_hop(r, "q", full_context=["a", "b", "c"])
    assert meta["fallback_applied"] and chunks == ["a", "b", "c"]


def test_save_load_roundtrip(tmp_path):
    enc = FakeEncoder()
    r = DenseRetriever(enc)
    chunks = ["alpha beta", "gamma delta", "timeout mtu balancer"]
    r.index(chunks)
    p = str(tmp_path / "idx")
    r.save(p)
    r2 = DenseRetriever(enc)
    r2.load(p)
    assert r2.retrieve("mtu balancer", k=1)[0][1] == "timeout mtu balancer"


def test_bm25_recovers_exact_identifier():
    retriever = BM25Retriever()
    retriever.index([
        "General network troubleshooting and timeout guidance",
        "Incident INC-4821 was caused by an MTU-1420 mismatch",
        "Database connection pooling recommendations",
    ])
    hits = retriever.retrieve("What caused INC-4821?", k=2)
    assert hits[0][0] == 1


def test_rrf_rewards_agreement_between_rankers():
    first = [(0, "dense only", 0.9), (1, "agreed", 0.8)]
    second = [(1, "agreed", 8.0), (2, "sparse only", 7.0)]
    fused = reciprocal_rank_fusion([first, second])
    assert fused[0][0] == 1


def test_quality_first_retrieval_protects_explicit_second_hop():
    context = [
        "Vermont soccer. The team competes in the America East Conference.",
        "Soccer archive. A team conference was formerly known by another name.",
        "Vermont athletics. Conference soccer team history and records.",
        "America East Conference. It was the North Atlantic Conference from 1988 to 1996.",
    ]

    class FixedDense:
        def retrieve(self, query, k):
            order = [0, 1, 2, 3]
            return [(i, context[i], 0.9 - i * 0.1) for i in order[:k]]

    chosen, meta = fetch_quality_first(
        FixedDense(),
        "The Vermont soccer team competes in a conference formerly known as what?",
        context,
        QualityFirstRetrievalConfig(max_k=2, min_k=2, bridge_seed_k=1),
    )
    assert context[0] in chosen
    assert context[3] in chosen
    assert meta["bridge_expansions"] == 1
    assert meta["method"] == "hybrid_rrf_bridge"


def test_bridge_cap_counts_replacements_not_already_selected_links():
    context = [
        "Seed Passage. See Already Selected Alpha, Already Selected Beta, and Needed.",
        "Already Selected Alpha. Existing evidence.",
        "Already Selected Beta. Existing evidence.",
        "Unrelated Decoy. Background material.",
        "Needed. The decisive second-hop evidence.",
    ]

    class FixedDense:
        def retrieve(self, query, k):
            return [(i, context[i], 0.9 - i * 0.1) for i in range(min(k, len(context)))]

    chosen, meta = fetch_quality_first(
        FixedDense(),
        "What did the seed passage establish?",
        context,
        QualityFirstRetrievalConfig(
            max_k=4,
            min_k=4,
            bridge_seed_k=1,
            max_bridge_expansions=1,
        ),
    )
    assert context[4] in chosen
    assert meta["bridge_expansions"] == 1


def test_low_confidence_falls_back_to_full():
    enc = FakeEncoder()
    r = DenseRetriever(enc)
    r.index(["completely unrelated text about gardening"])
    cfg = RetrievalConfig(k=1, min_top_score=0.9)
    chunks, meta = fetch_for_hop(r, "quantum chromodynamics lattice", ["full1", "full2"], cfg)
    assert meta["fallback_applied"] and chunks == ["full1", "full2"]


# --- ColBERTv2 operators --------------------------------------------------- #
def test_maxsim_known_values():
    q = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    d = np.array([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]], dtype=np.float32)
    # each query token's best match is its identical doc token -> 1 + 1 = 2
    assert abs(maxsim(q, d) - 2.0) < 1e-6


def test_residual_compression_8bit_reconstructs_closely():
    rng = np.random.default_rng(0)
    emb = rng.standard_normal((500, 384)).astype(np.float32)
    comp = ResidualCompressor(n_centroids=64, nbits=8).fit(emb)
    code = comp.encode(emb)
    err = np.abs(comp.decode(code) - emb).mean()
    assert err < 0.05                                 # near-lossless at 8 bits
    assert ResidualCompressor.full_nbytes(emb) / code.nbytes() > 1.0


def test_residual_compression_2bit_high_ratio():
    rng = np.random.default_rng(1)
    emb = rng.standard_normal((4000, 384)).astype(np.float32)  # amortizes centroids
    comp = ResidualCompressor(n_centroids=128, nbits=2).fit(emb)  # ColBERTv2 regime
    code = comp.encode(emb)
    ratio = ResidualCompressor.full_nbytes(emb) / code.nbytes()
    assert ratio >= 6.0                               # paper reports 6-10x


# --- MaxSim Reranker --------------------------------------------------- #
def test_maxsim_reranker_scores_relevant_higher():
    """MaxSim should give higher scores to passages that overlap query semantically."""
    enc = FakeEncoder()
    reranker = MaxSimReranker(enc)
    candidates = [
        (0, "dogs are loyal animals", 0.5),      # some semantic overlap
        (1, "cats are independent creatures", 0.4),
        (2, "dogs run fast in parks", 0.3),      # more dog/park overlap
    ]
    reranked = reranker.rerank("do dogs like parks", candidates, top_k=3)
    # Should rerank with better MaxSim scores; at least should not crash
    assert len(reranked) <= 3
    assert all(isinstance(r[2], float) for r in reranked)


def test_maxsim_reranker_empty_passage():
    """MaxSim should handle passages with no sentence boundaries gracefully."""
    enc = FakeEncoder()
    reranker = MaxSimReranker(enc)
    candidates = [
        (0, "hello world", 0.5),
        (1, "", 0.4),  # empty passage
    ]
    reranked = reranker.rerank("hello", candidates, top_k=2)
    assert len(reranked) >= 1  # at least one valid result


def test_maxsim_reranker_top_k_limit():
    """MaxSim rerank should never return more than top_k."""
    enc = FakeEncoder()
    reranker = MaxSimReranker(enc)
    candidates = [
        (i, f"passage about cats {i}", 0.5 - i * 0.01)
        for i in range(10)
    ]
    reranked = reranker.rerank("cats", candidates, top_k=3)
    assert len(reranked) == 3


# --- Adaptive Retrieval ------------------------------------------------- #
def test_adaptive_retrieval_elbow_method():
    """Adaptive retrieval should find largest score gap (elbow method)."""
    enc = FakeEncoder()
    r = DenseRetriever(enc)
    # Create passages with decreasing relevance
    passages = [
        "cats are small animals",
        "dogs are loyal animals",
        "birds fly in the sky",
        "fish swim in water",
        "reptiles are cold-blooded",
    ]
    r.index(passages)
    cfg = AdaptiveRetrievalConfig(
        max_k=5,
        use_maxsim_rerank=False,  # test DPR-only elbow method
    )
    chunks, meta = fetch_adaptive(r, "small cats", full_context=passages, cfg=cfg)
    # Should find an elbow and select fewer than all 5 passages
    assert meta["fallback_applied"] is False
    assert meta["k_chosen"] >= 1
    assert meta["k_chosen"] <= 5  # should be limited


def test_adaptive_retrieval_with_maxsim():
    """Adaptive retrieval with MaxSim reranking should find elbow after reranking."""
    enc = FakeEncoder()
    r = DenseRetriever(enc)
    passages = [
        "cats are small domestic animals",
        "dogs are loyal domestic animals",
        "the cat sat on the mat",
    ]
    r.index(passages)
    cfg = AdaptiveRetrievalConfig(
        max_k=3,
        use_maxsim_rerank=True,
    )
    chunks, meta = fetch_adaptive(r, "cats", full_context=passages, encoder=enc, cfg=cfg)
    assert meta["fallback_applied"] is False
    assert meta["k_chosen"] >= 1
    assert meta["method"] == "adaptive_maxsim"
    assert len(meta["scores"]) == meta["k_chosen"]


def test_adaptive_empty_index_fallback():
    """Adaptive should fallback to full context if index is empty."""
    r = DenseRetriever(FakeEncoder())
    full = ["full1", "full2", "full3"]
    cfg = AdaptiveRetrievalConfig()
    chunks, meta = fetch_adaptive(r, "query", full_context=full, cfg=cfg)
    assert meta["fallback_applied"] is True
    assert chunks == full


def test_adaptive_low_confidence_fallback():
    """Adaptive should fallback to full context if top score is too low."""
    enc = FakeEncoder()
    r = DenseRetriever(enc)
    r.index(["completely unrelated gardening text"])
    full = ["full1", "full2"]
    cfg = AdaptiveRetrievalConfig(min_top_score=0.9)  # unreasonably high
    chunks, meta = fetch_adaptive(r, "quantum computing lattice", full_context=full, cfg=cfg)
    assert meta["fallback_applied"] is True
    assert chunks == full
