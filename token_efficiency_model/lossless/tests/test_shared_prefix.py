"""Shared-prefix promotion (b9) + CACHE-AWARE gate (b9 v2).

b9 reorders shared context to a byte-identical leading prefix so it caches across agents
with distinct system prompts. v2 only does so when it would cache MORE tokens than the
provider already caches naturally (Don't Break the Cache arXiv:2601.06007 + CacheWeaver):
    reorder  iff  L_reorder > L_natural + max(min_gain_tokens, 2%·total)
Deterministic; no network.
"""
from __future__ import annotations

from token_efficiency_model.lossless.shared_prefix import SharedPrefixLayer

BIG = "shared reference material. " * 400        # ~1600 tokens (clears the 500 gate)


def _msgs(system, shared, task):
    return [{"role": "system", "content": system},
            {"role": "user", "content": shared},
            {"role": "user", "content": task}]


# --------------------------------------------------------------------------- #
# reorder mechanics (natural_cached=0 → gate open for a large shared block)
# --------------------------------------------------------------------------- #
def test_no_promotion_until_seen_across_two_agents():
    L = SharedPrefixLayer(min_agents=2)
    out1 = L.layout("pipe", "buffett", _msgs("You are Buffett.", BIG, "verdict?"))
    assert [m["content"] for m in out1][0] != BIG, "agent 1: not yet shared → no reorder"
    out2 = L.layout("pipe", "wood", _msgs("You are Wood.", BIG, "verdict?"))
    assert out2[0]["content"] == BIG, "agent 2: shared + big → promoted to front"
    assert out2[-1]["content"] == "verdict?", "volatile task stays last"


def test_promoted_prefix_byte_identical_across_agents():
    L = SharedPrefixLayer(min_agents=2)
    for a in ("buffett", "wood"):
        L.layout("p", a, _msgs(f"You are {a}.", BIG, "q"))
    leads = [L.layout("p", a, _msgs(f"You are {a}.", BIG, "q"))[0]["content"]
             for a in ("munger", "burry", "ackman")]
    assert len(set(leads)) == 1 and leads[0] == BIG


def test_lossless_same_message_set():
    L = SharedPrefixLayer(min_agents=1)
    msgs = _msgs("SYS", BIG, "TASK")
    L.layout("p", "a", msgs)
    out = L.layout("p", "a", _msgs("SYS", BIG, "TASK"))
    assert sorted((m["role"], m["content"]) for m in msgs) == \
           sorted((m["role"], m["content"]) for m in out)


def test_volatile_last_message_never_moved():
    L = SharedPrefixLayer(min_agents=1)
    L.layout("p", "a", _msgs("S", BIG, "Q1"))
    out = L.layout("p", "a", _msgs("S", BIG, "Q2-different"))
    assert out[-1]["content"] == "Q2-different"


def test_single_agent_session_not_reordered():
    L = SharedPrefixLayer(min_agents=2)
    for _ in range(3):
        out = L.layout("p", "sole", _msgs("SYS", BIG, "q"))
        assert out[0]["content"] != BIG      # never promoted for a lone agent


# --------------------------------------------------------------------------- #
# CACHE-AWARE gate (b9 v2) — the regression fix
# --------------------------------------------------------------------------- #
def test_small_shared_block_not_worth_reordering():
    # a tiny shared block (< min_gain) must NOT trigger a reorder even when shared
    L = SharedPrefixLayer(min_agents=2)
    L.layout("p", "a", _msgs("You are A.", "tiny", "q"))
    out = L.layout("p", "b", _msgs("You are B.", "tiny", "q"))
    assert out[0]["content"] != "tiny", "sub-500-token shared block: not worth reordering"


def test_no_reorder_when_provider_already_caches_well():
    # THE REGRESSION FIX: if the provider already caches a big natural prefix
    # (natural_cached high), do NOT reorder even though a shared block exists.
    L = SharedPrefixLayer(min_agents=2)
    for a in ("a", "b"):
        L.layout("p", a, _msgs(f"You are {a}.", BIG, "q"), natural_cached_tokens=0)
    # now the provider is observed caching ~5000 tokens naturally → leave it alone
    out = L.layout("p", "c", _msgs("You are c.", BIG, "q"), natural_cached_tokens=5000)
    assert out[0]["content"] != BIG, "must not reorder when natural cache already large"


def test_reorder_when_natural_cache_low_and_block_large():
    L = SharedPrefixLayer(min_agents=2)
    for a in ("a", "b"):
        L.layout("p", a, _msgs(f"You are {a}.", BIG, "q"), natural_cached_tokens=0)
    # provider barely caching (100 tok) + big shared block → reorder wins
    out = L.layout("p", "c", _msgs("You are c.", BIG, "q"), natural_cached_tokens=100)
    assert out[0]["content"] == BIG, "reorder when provider caches poorly and block is big"


def test_gate_uses_real_token_counter():
    # pass a real-ish counter; a big shared block with zero natural cache must reorder
    # (BIG promoted into the leading block; volatile Q stays last)
    L = SharedPrefixLayer(min_agents=2)
    ct = lambda t: len((t or "").split())
    for a in ("a", "b"):
        L.layout("p", a, _msgs(f"role {a}", BIG, "Q"), natural_cached_tokens=0, count_tokens=ct)
    out = L.layout("p", "c", _msgs("role c", BIG, "Q"), natural_cached_tokens=0, count_tokens=ct)
    assert out[0]["content"] == BIG and out[-1]["content"] == "Q"
