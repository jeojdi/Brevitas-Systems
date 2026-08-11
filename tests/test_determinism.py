"""Prefix determinism — the render must be byte-stable up to the last breakpoint.

WHY THIS FILE EXISTS. Every provider prefix cache matches on an EXACT byte prefix:
one differing byte early invalidates everything after it, and a request that should
have been a ~0.1x read (Anthropic; docs/ANTHROPIC_CACHE_MAP.md) is billed as a fresh
write instead — on Anthropic a 1.25x write, so the swing on a cached-prefix-heavy
agent is roughly 12x on the input side. A nondeterministic renderer therefore does
not fail loudly. It silently multiplies a customer's bill, and Brevitas bills 25% of
receipt-verified savings, so it silently deletes our revenue on the same request. The
failure surfaces three months later in an invoice, which is the worst possible place
to discover it. Nothing else in CI asserts this.

THE PROPERTY. Given a FIXED agent configuration (model, tools, system, prior turns)
and two DIFFERENT final user messages, everything the provider hashes up to the last
cache breakpoint must be byte-identical, and only the volatile tail may move. That is
what makes a cache entry reusable across turns and shareable across agents.

WHAT IS PINNED HERE, and what is not:

  * The identity used throughout is the one the product actually stores and matches
    on — brevitas/warming.py's canonical payload (prefix_hash) and its chain digests
    — not a test-local re-derivation. A test that invented its own serialization
    could pass while the shipped one drifted.
  * Idempotence under dict reuse. provider_cache.apply_anthropic_cache's docstring
    is explicit that real callers reuse message dicts across turns, so stale markers
    persist; a 5th cache_control block is a hard HTTP 400, so it strips ALL markers
    before placing fresh ones. Re-annotation must be a fixed point, in bytes.
  * The forks that MUST exist (TTL tier, vary headers). Collapsing them would let a
    5m row claim a 1h entry's read. Those assertions are safety pins: never relax
    them into "identity ignores X".
  * Two properties the code does NOT have are marked xfail(strict=True) with the gap
    named, rather than asserted as if they held. Both are reported to the caller.

Conventions follow tests/test_warm_chain.py (library-level, no store, no server;
private helpers imported directly since they are the real serialization boundary).
"""
from __future__ import annotations

import copy
import json

import pytest

from brevitas.warming import (
    _canon_strict,
    _capture_prefix,
    _last_marker,
    extract_warm_prefix,
)
from token_efficiency_model.lossless import engine as eng
from token_efficiency_model.lossless import provider_cache as pc
from token_efficiency_model.lossless.provider_cache import (
    apply_anthropic_cache,
    apply_openai_cache,
    count_cache_control,
    tokenizer_exact,
)
from token_efficiency_model.lossless.router import BrevitasRouter
from token_efficiency_model.lossless.shared_prefix import SharedPrefixLayer

# Sonnet is unlisted in _ANTHROPIC_MIN, so it carries the 1024-token default floor.
# Deliberately NOT Haiku 4.5: its measured 4096 floor (docs/ANTHROPIC_CACHE_MAP.md P3)
# would force every fixture over 4096 tokens to place a marker at all, making these
# tests slow for no added signal about determinism.
MODEL = "claude-sonnet-4-5-20250929"
OPENAI_MODEL = "gpt-5.6"          # the only family _openai_cache_capable() allows

requires_tokenizer = pytest.mark.skipif(
    not tokenizer_exact(),
    reason="chain digests are quarantined (None) without tiktoken — by design, "
           "see brevitas.warming.build_prefix_chain",
)


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_openai_key_shards():
    """provider_cache keeps PROCESS-WIDE shard state (_KEY_RPM/_KEY_SHARDS) keyed by
    tenant. It is rate-sensitive, so a burst in one test would change the key another
    test renders — the exact class of hidden cross-request coupling this file is about.
    Reset both directions so neither we nor anyone after us inherits a shard count."""
    pc._KEY_RPM.clear()
    pc._KEY_SHARDS.clear()
    yield
    pc._KEY_RPM.clear()
    pc._KEY_SHARDS.clear()


def _big(seed: str, words: int = 900) -> str:
    """Distinct tokens, not repeated filler: repeated filler compresses in the
    tokenizer and can slip under a min_tokens floor without the test noticing."""
    return " ".join(f"{seed}{i}" for i in range(words))


# One FIXED agent configuration, defined once and deep-copied per request, exactly as
# a real agent framework would: same tools, same system prompt, same prior turns.
_TOOLS = [
    {"name": "search", "description": _big("searchdoc", 300),
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {"name": "write_file", "description": _big("writedoc", 300),
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
]
_SYSTEM = [{"type": "text", "text": _big("system", 900)}]
_HISTORY = [
    {"role": "user", "content": [{"type": "text", "text": _big("turn1", 800)}]},
    {"role": "assistant", "content": [{"type": "text", "text": _big("reply1", 400)}]},
]


def _agent_body(tail: str, *, tools=None, system=None, model: str = MODEL) -> dict:
    """A request from the fixed agent config above, varying only the final user turn."""
    return {
        "model": model,
        "tools": copy.deepcopy(_TOOLS if tools is None else tools),
        "system": copy.deepcopy(_SYSTEM if system is None else system),
        "messages": copy.deepcopy(_HISTORY) + [{"role": "user", "content": tail}],
    }


def _prefix_bytes(body: dict) -> bytes:
    """Exactly what the provider caches: every segment through the LAST breakpoint,
    canonicalized with warming's own strict serializer. Deliberately not a re-implementation
    — _last_marker/_capture_prefix are the shipped definition of "the cached prefix"."""
    found = _last_marker(body)
    assert found is not None, "no cache_control marker placed — fixture is below the floor"
    return _canon_strict(_capture_prefix(body, found[0]))


def _marker_position(body: dict):
    found = _last_marker(body)
    assert found is not None
    return found[0]


def _warm(body: dict, plan, headers=None):
    """The stored warm-prefix identity for an already-annotated Anthropic body."""
    prefix = extract_warm_prefix(
        body, "anthropic", str(body.get("model", "")), headers or {},
        {"cached_prefix_tokens": plan.cached_prefix_tokens})
    assert prefix is not None, "extract_warm_prefix declined — fixture below the floor"
    return prefix


def _assert_requests_really_differ(a: dict, b: dict) -> None:
    """Guard against a vacuous pass: if the two requests were identical, every
    'prefix is stable' assertion below would hold trivially and prove nothing."""
    assert json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)


# --------------------------------------------------------------------------- #
# 1. The headline property: fixed config + different user messages => same prefix
# --------------------------------------------------------------------------- #
def test_prefix_bytes_identical_across_different_user_messages():
    short = _agent_body("What is the capital of France?")
    long = _agent_body("An entirely different question, at length. " * 40)
    plan_a = apply_anthropic_cache(short)
    plan_b = apply_anthropic_cache(long)
    _assert_requests_really_differ(short, long)

    # The whole cross-turn and cross-agent cache-sharing mechanism is this one line.
    assert _prefix_bytes(short) == _prefix_bytes(long)
    # ...and the breakpoint must land in the same place, or the second request caches
    # a shorter prefix than the first and re-pays for the difference.
    assert _marker_position(short) == _marker_position(long)
    assert plan_a.positions == plan_b.positions
    assert plan_a.cached_prefix_tokens == plan_b.cached_prefix_tokens


def test_stored_prefix_hash_identical_across_different_user_messages():
    a = _agent_body("q1")
    b = _agent_body("a much longer and completely unrelated second question " * 25)
    wa = _warm(a, apply_anthropic_cache(a))
    wb = _warm(b, apply_anthropic_cache(b))
    # prefix_hash is the primary key of warm_prefixes (api/store.py). If it moved with
    # the user's message, every request would mint a NEW warm row: the arrival history
    # that predicts the next call never accumulates, and the worker warms prefixes that
    # will never be read again.
    assert wa.prefix_hash == wb.prefix_hash
    assert wa.prefix_tokens == wb.prefix_tokens
    assert wa.provider_ttl_seconds == wb.provider_ttl_seconds


@requires_tokenizer
def test_chain_digests_identical_across_different_user_messages():
    a = _agent_body("q1")
    b = _agent_body("q2, which is a good deal longer than q1 " * 30)
    wa = _warm(a, apply_anthropic_cache(a))
    wb = _warm(b, apply_anthropic_cache(b))
    assert wa.chain is not None and wb.chain is not None
    # The chain is what makes containment visible across requests that share a leading
    # prefix (brevitas/warming.py "Chain-hash prefix keying"). A tail-sensitive chain
    # root would put every request in its own dedup group and fragment attribution.
    assert wa.chain.digests == wb.chain.digests
    assert wa.chain.block_tokens == wb.chain.block_tokens
    assert wa.chain.token_cum == wb.chain.token_cum


def test_openai_stable_prefix_identity_survives_a_different_tail():
    """Automatic-cache providers get no markers, so the identity is the captured
    stable view (_extract_auto_prefix). Same property, different code path."""
    def body(tail: str) -> dict:
        return {"model": OPENAI_MODEL, "messages": [
            {"role": "system", "content": _big("osys", 900)},
            {"role": "user", "content": _big("ouser", 900)},
            {"role": "assistant", "content": _big("oassist", 500)},
            {"role": "user", "content": tail}]}

    a, b = body("short"), body("a substantially different closing turn " * 30)
    upstream = "https://api.openai.com/v1/chat/completions"
    wa = extract_warm_prefix(a, "openai", OPENAI_MODEL, {}, {}, upstream=upstream)
    wb = extract_warm_prefix(b, "openai", OPENAI_MODEL, {}, {}, upstream=upstream)
    assert wa is not None and wb is not None
    _assert_requests_really_differ(a, b)
    assert wa.prefix_hash == wb.prefix_hash
    assert wa.prefix_tokens == wb.prefix_tokens


# --------------------------------------------------------------------------- #
# 2. Idempotence under reuse (the HTTP 400 / double-billing pair)
# --------------------------------------------------------------------------- #
def test_reannotating_the_same_body_is_a_byte_level_fixed_point():
    body = _agent_body("q")
    apply_anthropic_cache(body)
    once = json.dumps(body, sort_keys=True)
    apply_anthropic_cache(body)
    twice = json.dumps(body, sort_keys=True)
    apply_anthropic_cache(body)
    thrice = json.dumps(body, sort_keys=True)
    # Not merely "still valid" — identical. A body that drifts on each pass through
    # the engine (a retry, a middleware that annotates twice) busts its own cache.
    assert once == twice == thrice
    assert count_cache_control(body) <= 4    # a 5th block is a hard HTTP 400


def test_reused_message_dicts_keep_the_prefix_and_the_four_breakpoint_ceiling():
    """The documented real-caller pattern: message dicts (and their content LISTS)
    are reused turn over turn, so markers from previous turns are still attached."""
    tools = copy.deepcopy(_TOOLS)
    system = copy.deepcopy(_SYSTEM)
    history = copy.deepcopy(_HISTORY)      # reused by reference across every turn
    baseline = None
    for turn in range(1, 8):
        body = {"model": MODEL, "tools": tools, "system": system,
                # dict(m) copies the message dict but NOT its content list, exactly
                # as an append-only history in a real agent loop does.
                "messages": [dict(m) for m in history] + [
                    {"role": "user", "content": f"question number {turn}?"}]}
        apply_anthropic_cache(body)
        assert count_cache_control(body) <= 4, f"turn {turn} would 400"
        current = _prefix_bytes(body)
        if baseline is None:
            baseline = current
        else:
            # The config did not change, so turn 7 must hit the entry turn 1 wrote.
            assert current == baseline, f"turn {turn} moved the cached prefix"


def test_growing_history_matches_a_fresh_build_and_never_exceeds_four_markers():
    """The realistic version of the above: the history GROWS, so the four best
    breakpoints move forward every turn and last turn's markers are now in the wrong
    places. Two things have to hold at once — the marker count must stay under
    Anthropic's hard limit of 4 (a 5th is an HTTP 400, not a degraded cache), and the
    render must be a function of the BODY, not of how many times it has been annotated.
    A caller replaying a conversation from a fresh transcript and a caller who kept the
    live dicts in memory must send the same bytes, or the second one misses the entry
    the first one paid to write."""
    tools = copy.deepcopy(_TOOLS)          # accumulates markers across turns
    system = copy.deepcopy(_SYSTEM)
    accumulated_history: list = []
    pristine_history: list = []
    for turn in range(1, 6):
        for role, seed, words in (("user", f"grow{turn}", 700),
                                  ("assistant", f"ans{turn}", 300)):
            block = {"type": "text", "text": _big(seed, words)}
            accumulated_history.append({"role": role, "content": [dict(block)]})
            pristine_history.append({"role": role, "content": [dict(block)]})
        tail = {"role": "user", "content": f"question {turn}"}

        # dict(m) shares the content LIST, so prior turns' markers are still attached.
        accumulated = {"model": MODEL, "tools": tools, "system": system,
                       "messages": [dict(m) for m in accumulated_history] + [dict(tail)]}
        fresh = {"model": MODEL, "tools": copy.deepcopy(_TOOLS),
                 "system": copy.deepcopy(_SYSTEM),
                 "messages": copy.deepcopy(pristine_history) + [dict(tail)]}
        apply_anthropic_cache(accumulated)
        apply_anthropic_cache(fresh)

        assert count_cache_control(accumulated) <= 4, f"turn {turn} would 400"
        assert _prefix_bytes(accumulated) == _prefix_bytes(fresh), (
            f"turn {turn}: annotation history changed the rendered prefix")


def test_str_system_converts_once_and_then_renders_identically():
    """apply_anthropic_cache rewrites a string `system` into a one-block list to carry
    the marker. That rewrite is byte-visible, so it must converge after ONE pass —
    otherwise a caller who reuses the body renders different bytes than one who does not."""
    fresh = {"model": MODEL, "system": _big("sharedsys", 900),
             "messages": copy.deepcopy(_HISTORY) + [{"role": "user", "content": "q"}]}
    reused = copy.deepcopy(fresh)

    apply_anthropic_cache(fresh)                       # first pass: str -> [block]
    apply_anthropic_cache(reused)
    apply_anthropic_cache(reused)                      # second pass on the rewritten body
    # Different call histories, identical rendered prefix.
    assert _prefix_bytes(fresh) == _prefix_bytes(reused)
    assert json.dumps(fresh, sort_keys=True) == json.dumps(reused, sort_keys=True)


def test_annotated_and_unannotated_callers_agree_on_the_cached_prefix():
    """A body already carrying stale markers must land on the SAME prefix as a body
    built clean this turn — that is what _strip_cache_control exists to guarantee."""
    clean = _agent_body("q")
    stale = _agent_body("q")
    # Simulate a previous turn's markers sitting in the wrong (earlier) places.
    stale["tools"][0]["cache_control"] = {"type": "ephemeral"}
    stale["system"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    apply_anthropic_cache(clean)
    apply_anthropic_cache(stale)
    assert _prefix_bytes(clean) == _prefix_bytes(stale)
    assert count_cache_control(clean) == count_cache_control(stale) <= 4


# --------------------------------------------------------------------------- #
# 3. Tool ordering — ACTUAL behavior, not the behavior we might prefer
# --------------------------------------------------------------------------- #
def test_same_tool_order_renders_the_same_prefix():
    """Positive control for the pair below: order held fixed, identity holds."""
    a = _agent_body("q1", tools=_TOOLS)
    b = _agent_body("q2 entirely different " * 20, tools=_TOOLS)
    apply_anthropic_cache(a)
    apply_anthropic_cache(b)
    assert _prefix_bytes(a) == _prefix_bytes(b)


def test_tool_order_is_load_bearing_and_is_not_canonicalized():
    """MEASURED behavior: nothing in the render path sorts or normalizes `tools`.

    This is faithful to the provider — the serialized tool definitions really are part
    of the cached byte prefix, so a reordered tool array really is a cache miss upstream
    — and the identity correctly refuses to claim the two share an entry. The cost is
    that a caller who builds `tools` from a set, a dict view, or a plugin-discovery
    walk gets a fresh full-price write on EVERY request, and no code path here notices.
    See the xfail below for the gap that follows from this.
    """
    forward = _agent_body("q", tools=_TOOLS)
    reversed_ = _agent_body("q", tools=list(reversed(_TOOLS)))
    plan_f = apply_anthropic_cache(forward)
    plan_r = apply_anthropic_cache(reversed_)
    # Same tool SET, same token weight, same breakpoint plan...
    assert plan_f.positions == plan_r.positions
    assert plan_f.cached_prefix_tokens == plan_r.cached_prefix_tokens
    # ...and a different cached prefix, because the bytes differ.
    assert _prefix_bytes(forward) != _prefix_bytes(reversed_)
    assert _warm(forward, plan_f).prefix_hash != _warm(reversed_, plan_r).prefix_hash


@pytest.mark.xfail(
    strict=True,
    reason="GAP (documented, not queued): no tool-order canonicalization exists. "
           "Sorting `tools` would make an unstable caller cache-stable, but it also "
           "changes what the model sees — tool order is known to bias selection — so "
           "it is NOT a free lossless transform and must not be added silently. If "
           "this test XPASSes, someone added normalization: prove losslessness first, "
           "then delete this marker.",
)
def test_tool_order_normalization_is_not_implemented():
    forward = _agent_body("q", tools=_TOOLS)
    reversed_ = _agent_body("q", tools=list(reversed(_TOOLS)))
    apply_anthropic_cache(forward)
    apply_anthropic_cache(reversed_)
    assert _prefix_bytes(forward) == _prefix_bytes(reversed_)


# --------------------------------------------------------------------------- #
# 4. Canonical JSON — the serialization that feeds the prefix hash
# --------------------------------------------------------------------------- #
def test_prefix_identity_is_insensitive_to_dict_key_insertion_order():
    """Two builders emitting the same request with keys in different insertion order
    (a dataclass asdict() vs a hand-built literal, say) must hash the same. This is
    what sort_keys=True buys, and it is easy to lose to a hand-rolled serializer."""
    natural = _agent_body("q")
    shuffled = {
        "messages": [
            {"content": [{"text": _big("turn1", 800), "type": "text"}], "role": "user"},
            {"content": [{"text": _big("reply1", 400), "type": "text"}],
             "role": "assistant"},
            {"content": "q", "role": "user"},
        ],
        "system": [{"text": _big("system", 900), "type": "text"}],
        "tools": [
            {"input_schema": {"properties": {"q": {"type": "string"}}, "type": "object"},
             "description": _big("searchdoc", 300), "name": "search"},
            {"input_schema": {"properties": {"path": {"type": "string"}},
                              "type": "object"},
             "description": _big("writedoc", 300), "name": "write_file"},
        ],
        "model": MODEL,
    }
    wa = _warm(natural, apply_anthropic_cache(natural))
    wb = _warm(shuffled, apply_anthropic_cache(shuffled))
    assert wa.prefix_hash == wb.prefix_hash
    if wa.chain is not None and wb.chain is not None:
        assert wa.chain.digests == wb.chain.digests


def test_canonical_form_carries_no_incidental_whitespace():
    """separators=(",", ":") is load-bearing across the whole product: falling back to
    json.dumps' default ", " / ": " changes EVERY digest at once, which orphans every
    stored warm_prefixes row and every chain node in one deploy. Pinned literally."""
    assert _canon_strict({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'
    assert b", " not in _canon_strict({"a": [1, 2], "b": {"c": 3, "d": 4}})
    assert b'": ' not in _canon_strict({"a": 1, "b": 2})


def test_canon_strict_refuses_a_non_serializable_element():
    """No default= escape hatch: str(obj) on most objects embeds a memory ADDRESS, so
    a structural key built through it would differ per process and match nothing."""
    class Opaque:
        pass

    with pytest.raises(TypeError):
        _canon_strict({"tools": [Opaque()]})


@requires_tokenizer
def test_chain_is_quarantined_rather_than_approximated_on_non_json_payloads():
    body = _agent_body("q")
    plan = apply_anthropic_cache(body)
    body["messages"][0]["content"][0]["vendor_handle"] = object()
    prefix = _warm(body, plan)
    # chain=None means "no structural key for this observation" — the correct failure.
    # A best-effort chain here would seed dedup groups with process-local addresses.
    assert prefix.chain is None


@pytest.mark.xfail(
    strict=True,
    reason="GAP: prefix_hash still canonicalizes with json.dumps(default=str), so a "
           "non-JSON value anywhere in the payload hashes its repr — which for most "
           "objects embeds a memory address — and the same logical request gets a new "
           "prefix_hash every process. LATENT today: the hosted path "
           "(brevitas/proxy.py::_deliver_warm_prefix) only ever passes a body parsed "
           "off the wire. It becomes live the moment an in-process SDK wrapper hands "
           "extract_warm_prefix a body holding Python objects. The chain already "
           "quarantines this case (_canon_strict, test above); prefix_hash does not.",
)
def test_prefix_hash_is_process_stable_for_non_json_payloads():
    class Opaque:
        pass

    def body_with_object() -> dict:
        body = _agent_body("q")
        plan = apply_anthropic_cache(body)
        body["messages"][0]["content"][0]["vendor_handle"] = Opaque()
        return _warm(body, plan).prefix_hash

    # Two logically identical requests; only the object identity differs.
    assert body_with_object() == body_with_object()


# --------------------------------------------------------------------------- #
# 5. The volatile tail is free to move — and only the tail
# --------------------------------------------------------------------------- #
def test_final_block_of_the_last_user_message_never_carries_a_marker():
    body = {"model": MODEL, "system": copy.deepcopy(_SYSTEM),
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": _big("document", 2000)},
                {"type": "text", "text": "and what does it say about revenue?"}]}]}
    apply_anthropic_cache(body)
    tail_block = body["messages"][-1]["content"][-1]
    # Marking the volatile block caches content that changes every request: a 1.25x
    # write premium paid for an entry that can never be read.
    assert "cache_control" not in tail_block
    assert count_cache_control(body) >= 1        # the stable document IS marked


def test_content_below_the_last_breakpoint_moves_without_moving_the_prefix():
    a = _agent_body("q")
    b = _agent_body("q")
    b["messages"][-1]["content"] = "a completely different closing question " * 50
    plan_a = apply_anthropic_cache(a)
    plan_b = apply_anthropic_cache(b)
    _assert_requests_really_differ(a, b)
    # The bytes AFTER the breakpoint differ; the bytes BEFORE it do not.
    assert a["messages"][-1]["content"] != b["messages"][-1]["content"]
    assert _prefix_bytes(a) == _prefix_bytes(b)
    assert _warm(a, plan_a).prefix_hash == _warm(b, plan_b).prefix_hash


# --------------------------------------------------------------------------- #
# 6. Forks that MUST stay forked (safety pins — never relax into "identity ignores X")
# --------------------------------------------------------------------------- #
def test_ttl_tier_forks_the_prefix_identity():
    """5m and 1h are separate provider cache entries with separate write premiums
    (1.25x vs 2x — docs, and the 1h tier is confirmed live in ANTHROPIC_CACHE_MAP.md).
    Identical prompt bytes on different tiers are NOT the same cached thing, so the
    stored identity must fork even though the prompt text is byte-identical."""
    five_min = _agent_body("q")
    one_hour = _agent_body("q")
    w5 = _warm(five_min, apply_anthropic_cache(five_min, ttl=""))
    w1h = _warm(one_hour, apply_anthropic_cache(one_hour, ttl="1h"))
    assert w5.prefix_hash != w1h.prefix_hash
    assert (w5.provider_ttl_seconds, w1h.provider_ttl_seconds) == (300, 3600)


def test_vary_headers_fork_the_prefix_identity():
    """Headers that change how the provider interprets the same bytes address a
    different entry; merging them would let one row claim another's read."""
    body = _agent_body("q")
    plan = apply_anthropic_cache(body)
    plain = _warm(copy.deepcopy(body), plan)
    varied = _warm(copy.deepcopy(body), plan,
                   headers={"anthropic-beta": "context-1m-2025-08-07"})
    assert plain.prefix_hash != varied.prefix_hash


# --------------------------------------------------------------------------- #
# 7. Cross-agent sharing: the promoted shared block is byte-identical per agent
# --------------------------------------------------------------------------- #
def test_promoted_shared_prefix_is_byte_identical_across_agents():
    """shared_prefix.py exists so N agents with DIFFERENT roles can share one cached
    prefix. If the promoted block's order or content varied per agent, every agent
    would write its own entry and the mechanism would cost money instead of saving it."""
    layer = SharedPrefixLayer()
    shared = _big("brief", 900)

    def count(text: str) -> int:
        return max(1, int(len((text or "").split()) * 1.3))

    def turn(tail: str) -> list:
        return [{"role": "user", "content": shared},
                {"role": "user", "content": tail}]

    layer.layout("pipeline", "agent_a", turn("agent A's own task"),
                 natural_cached_tokens=0.0, count_tokens=count)
    out_b = layer.layout("pipeline", "agent_b", turn("agent B's very different task"),
                         natural_cached_tokens=0.0, count_tokens=count)
    out_c = layer.layout("pipeline", "agent_c", turn("agent C, different again " * 10),
                         natural_cached_tokens=0.0, count_tokens=count)
    assert out_b[0] == out_c[0]                  # identical leading bytes
    assert out_b[-1] != out_c[-1]                # genuinely different tails
    assert _canon_strict(out_b[:-1]) == _canon_strict(out_c[:-1])


# --------------------------------------------------------------------------- #
# 8. OpenAI routing key: metadata, but it addresses the machine holding the prefix
# --------------------------------------------------------------------------- #
def _openai_body(tail: str, system_seed: str = "keysys") -> dict:
    return {"model": OPENAI_MODEL, "messages": [
        {"role": "system", "content": _big(system_seed, 900)},
        {"role": "user", "content": _big("keyuser", 900)},
        {"role": "assistant", "content": _big("keyassist", 500)},
        {"role": "user", "content": tail}]}


def test_openai_cache_key_is_stable_across_different_user_messages():
    """One prompt_cache_key routes to one machine. A per-request key would send each
    turn to a different machine and the residency the key exists to buy is gone."""
    tenant = "a" * 64
    keys = []
    for tail in ("q1", "q2 which is longer " * 30, "q3"):
        body = _openai_body(tail)
        apply_openai_cache(body, tenant_key=tenant, inject_key=True)
        keys.append(body.get("prompt_cache_key"))
    assert len(set(keys)) == 1 and keys[0] is not None


def test_sharded_key_follows_the_stable_prefix_not_round_robin():
    """Under load the key splits into shards. The shard index must be a hash of the
    STABLE prefix, so a prefix family keeps landing on the machine already holding it;
    round-robin would fragment exactly the residency being paid for."""
    tenant = "b" * 64
    for _ in range(200):                     # drive the EWMA over _KEY_RPM_PER_SHARD
        burst = _openai_body("burst")
        apply_openai_cache(burst, tenant_key=tenant, inject_key=True)
    assert pc._KEY_SHARDS[f"bx1:{tenant[:16]}"][0] > 1, "fixture failed to shard"

    keys = []
    for tail in ("q1", "q2 different tail " * 30, "q3"):
        body = _openai_body(tail)
        apply_openai_cache(body, tenant_key=tenant, inject_key=True)
        keys.append(body["prompt_cache_key"])
    # Same stable prefix, different tails -> one shard.
    assert len(set(keys)) == 1


# --------------------------------------------------------------------------- #
# 9. End to end through the engine (the path a real request takes)
# --------------------------------------------------------------------------- #
def test_engine_render_is_prefix_stable_for_a_fixed_config(monkeypatch):
    """Everything above tests the annotator directly. This runs the actual engine
    entry point, where the router, the ROI gate and the TTL-tier chooser also get a
    vote, and asserts the same property survives all of them."""
    monkeypatch.delenv("BREVITAS_ANTHROPIC_CACHE", raising=False)   # default ON
    monkeypatch.setenv("BREVITAS_TEMPLATE_SPLIT", "0")             # pin the CR2 split off
    # seed=0 removes the router's epsilon exploration as a source of variance; a list
    # `system` keeps the template miner (which is session-stateful) out of the path.
    router = BrevitasRouter(provider="anthropic", seed=0)
    session = "determinism-session"

    # Two calls first: apply_anthropic_cache only runs once the router has evidence the
    # prefix repeats (cache_write_allowed -> "reuse_unproven" before that).
    for warmup in ("warmup one", "warmup two"):
        eng.optimize_request(_agent_body(warmup), "anthropic", router, session)

    first = _agent_body("what does the document say about Q3?")
    second = _agent_body("unrelated: summarize the risks section instead " * 20)
    meta_a = eng.optimize_request(first, "anthropic", router, session)
    meta_b = eng.optimize_request(second, "anthropic", router, session)

    assert meta_a["cache_breakpoints"] >= 1, meta_a
    assert meta_a["cache_breakpoints"] == meta_b["cache_breakpoints"]
    assert meta_a["cached_prefix_tokens"] == meta_b["cached_prefix_tokens"]
    _assert_requests_really_differ(first, second)
    assert _prefix_bytes(first) == _prefix_bytes(second)


def test_engine_leaves_automatic_cache_providers_byte_identical():
    """OpenAI/DeepSeek cache byte-identical prefixes automatically, so the only safe
    render is no render at all. A marker injected here is both an unsupported field
    and, on the prefix side, a byte that guarantees a miss."""
    body = {"model": "deepseek-chat", "messages": [
        {"role": "user", "content": [{"type": "text", "text": _big("ds", 2000)},
                                     {"type": "text", "text": "Q?"}]}]}
    before = json.dumps(body, sort_keys=True)
    eng.optimize_request(body, "deepseek", BrevitasRouter(provider="deepseek", seed=0),
                         "ds-session")
    assert json.dumps(body, sort_keys=True) == before
