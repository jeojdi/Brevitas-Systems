"""Shared-prefix promotion (brief b9): lossless message reorder that hoists a
pipeline's shared context to a byte-identical leading prefix so it caches across
agents with distinct system prompts. Deterministic; no network."""
from __future__ import annotations

from token_efficiency_model.lossless.shared_prefix import SharedPrefixLayer, _norm


def _msgs(system, shared, task):
    return [{"role": "system", "content": system},
            {"role": "user", "content": shared},
            {"role": "user", "content": task}]


def test_no_promotion_until_seen_across_two_agents():
    L = SharedPrefixLayer(min_agents=2)
    # agent 1: shared context not yet proven shared → no reorder
    out1 = L.layout("pipe", "buffett", _msgs("You are Buffett.", "FACTS", "verdict?"))
    assert [m["content"] for m in out1] == ["You are Buffett.", "FACTS", "verdict?"]
    # agent 2 sends the SAME shared block → now it's shared; promoted to front
    out2 = L.layout("pipe", "wood", _msgs("You are Wood.", "FACTS", "verdict?"))
    assert out2[0]["content"] == "FACTS", "shared block must lead once proven shared"
    assert out2[-1]["content"] == "verdict?", "volatile task stays last"


def test_promoted_prefix_is_byte_identical_across_agents():
    L = SharedPrefixLayer(min_agents=2)
    facts = "10-K FACT SHEET " * 50
    for a in ("buffett", "wood"):                       # prime: seen by 2 agents
        L.layout("p", a, _msgs(f"You are {a}.", facts, "q"))
    leads = []
    for a in ("munger", "burry", "ackman"):
        out = L.layout("p", a, _msgs(f"You are {a}.", facts, "q"))
        # the leading shared portion must be identical bytes for every agent
        leads.append(out[0]["content"])
    assert len(set(leads)) == 1 and leads[0] == facts


def test_explicit_registration_promotes_immediately():
    L = SharedPrefixLayer(min_agents=2)
    facts = "shared brief"
    L.register_shared("p", facts)
    out = L.layout("p", "solo", _msgs("You are an agent.", facts, "do it"))
    assert out[0]["content"] == facts   # promoted on the very first call


def test_lossless_same_message_set():
    L = SharedPrefixLayer(min_agents=1)   # promote as soon as seen
    msgs = _msgs("SYS", "SHARED", "TASK")
    L.layout("p", "a", msgs)
    out = L.layout("p", "a", _msgs("SYS", "SHARED", "TASK"))
    # exactly the same multiset of (role, content) — nothing added/dropped/altered
    before = sorted((m["role"], m["content"]) for m in msgs)
    after = sorted((m["role"], m["content"]) for m in out)
    assert before == after


def test_volatile_last_message_never_moved():
    L = SharedPrefixLayer(min_agents=1)
    facts = "F" * 100
    L.layout("p", "a", _msgs("S", facts, "Q1"))
    out = L.layout("p", "a", _msgs("S", facts, "Q2-different"))
    assert out[-1]["content"] == "Q2-different"


def test_single_agent_session_not_reordered():
    # one agent repeating (a chatbot) must NOT be reshuffled — min_agents guards this
    L = SharedPrefixLayer(min_agents=2)
    for _ in range(3):
        out = L.layout("p", "sole-agent", _msgs("SYS", "CTX", "q"))
        assert [m["content"] for m in out] == ["SYS", "CTX", "q"]
