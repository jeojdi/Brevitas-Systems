"""Chain-hash prefix keying (Phase 1.5 TASK A), pure brevitas/warming.py.

Everything here is library-level: no store, no server, no salt. The properties
the rest of Phase 1.5 rests on are structural -- prefix-closure, element-aligned
blocks, exact telescoping token counts -- plus the two stand-down rules that
keep a degraded run from writing rows nobody can trust.
"""
import hashlib
import json

import pytest

from brevitas import warming
from brevitas.warming import (
    _CHAIN_BLOCK_BYTES,
    _CHAIN_SCHEME,
    _canon_strict,
    extract_warm_prefix,
)
from token_efficiency_model.lossless.provider_cache import count_tokens

MODEL = "claude-sonnet-4-5-20250929"


def _filler(seed: str, words: int = 900) -> str:
    return " ".join(f"{seed}-{i}" for i in range(words))


def _anthropic_body(turns: int = 4, tool_choice=None):
    """A marked anthropic request whose captured prefix spans several blocks."""
    messages = []
    for i in range(turns):
        messages.append({"role": "user", "content": [
            {"type": "text", "text": _filler(f"u{i}")}]})
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": _filler(f"a{i}")}]})
    # Mark the last block of the last stable message: everything through it is
    # the cached prefix.
    messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    messages.append({"role": "user", "content": "volatile tail"})
    body = {
        "model": MODEL,
        "system": [{"type": "text", "text": _filler("system", 1200)}],
        "messages": messages,
    }
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def _openai_body(turns: int = 4, cache_key=None):
    messages = []
    for i in range(turns):
        messages.append({"role": "user", "content": _filler(f"u{i}")})
        messages.append({"role": "assistant", "content": _filler(f"a{i}")})
    messages.append({"role": "user", "content": "volatile tail"})
    body = {"model": "gpt-5.6", "messages": messages}
    if cache_key is not None:
        body["prompt_cache_key"] = cache_key
    return body


def _extract_anthropic(body, meta=None):
    return extract_warm_prefix(body, "anthropic", MODEL, {},
                               meta or {"cache_control_owner": "caller"})


def _extract_openai(body):
    return extract_warm_prefix(body, "openai", "gpt-5.6", {}, {},
                               upstream="https://api.openai.com/v1/chat/completions")


def test_chain_prefix_closed_under_extension():
    """The whole point: extending a conversation must EXTEND the digest list,
    not replace it. prefix_hash cannot do this and that is the containment bug.

    Written on the automatic-provider shape because that is where an extension
    is purely additive -- see the anthropic test below for the one place a
    marker moving forward forks a block."""
    short = _extract_openai(_openai_body(turns=4))
    longer = _extract_openai(_openai_body(turns=6))
    assert short is not None and longer is not None
    assert short.chain is not None and longer.chain is not None
    assert len(longer.chain.digests) > len(short.chain.digests)
    assert (longer.chain.digests[:len(short.chain.digests)]
            == short.chain.digests)
    # ... while the whole-payload hashes are unrelated, which is the gap.
    assert short.prefix_hash != longer.prefix_hash


def test_chain_anthropic_extension_shares_every_unmarked_block():
    """Anthropic's marker rides INSIDE the last captured block, so moving it
    forward one turn necessarily forks the block that contains it -- and
    nothing before it. Containment above that boundary is what the tree needs,
    and it is exact."""
    short = _extract_anthropic(_anthropic_body(turns=4))
    longer = _extract_anthropic(_anthropic_body(turns=6))
    assert short.chain is not None and longer.chain is not None
    shared = len(short.chain.digests) - 1
    assert shared >= 2
    assert longer.chain.digests[:shared] == short.chain.digests[:shared]
    assert short.prefix_hash != longer.prefix_hash


def test_chain_blocks_element_aligned_and_byte_budgeted():
    prefix = _extract_anthropic(_anthropic_body(turns=6))
    chain = prefix.chain
    assert chain is not None and len(chain.digests) >= 3
    # Every closed block consumed at least one whole element, and no element is
    # split: the per-block element counts sum to a prefix of the element list.
    assert all(count >= 1 for count in chain.block_elements)
    elements = warming._chain_elements("anthropic", {
        "tools": prefix.payload["tools"],
        "system": prefix.payload["system"],
        "messages_prefix": prefix.payload["messages_prefix"]})
    assert sum(chain.block_elements) <= len(elements)
    # Re-frame the elements and confirm each closed block reached the budget
    # (only the tail, which emits no digest, may be under it).
    cursor = 0
    for count in chain.block_elements:
        framed = b""
        for element in elements[cursor:cursor + count]:
            canonical = _canon_strict(element)
            framed += len(canonical).to_bytes(4, "big") + canonical
        assert len(framed) >= _CHAIN_BLOCK_BYTES
        cursor += count


def test_chain_token_counts_telescope_exactly():
    prefix = _extract_anthropic(_anthropic_body(turns=6))
    chain = prefix.chain
    assert chain is not None and chain.token_cum
    assert sum(chain.block_tokens) == chain.token_cum[-1]
    elements = warming._chain_elements("anthropic", {
        "tools": prefix.payload["tools"],
        "system": prefix.payload["system"],
        "messages_prefix": prefix.payload["messages_prefix"]})
    consumed = "".join(
        _canon_strict(element).decode("utf-8")
        for element in elements[:sum(chain.block_elements)])
    assert count_tokens(consumed) == chain.token_cum[-1]
    assert chain.tail_tokens == max(0, prefix.prefix_tokens - chain.token_cum[-1])


def test_chain_strict_canon_refuses_nonserializable():
    """default=str would hash a memory address into a STRUCTURAL key. The chain
    stands down instead; prefix_hash keeps its existing (tolerant) behaviour."""
    class Opaque:
        pass

    body = _anthropic_body(turns=4)
    body["system"].append({"type": "text", "text": "x", "extra": Opaque()})
    prefix = _extract_anthropic(body)
    assert prefix is not None
    assert prefix.chain is None
    assert len(prefix.prefix_hash) == 64


def test_chain_identity_extras_fork_the_seed():
    plain = _extract_anthropic(_anthropic_body(turns=4))
    forked = _extract_anthropic(_anthropic_body(turns=4, tool_choice={"type": "any"}))
    assert plain.chain.digests[0] != forked.chain.digests[0]
    # ... and every downstream digest with it: the seed is chained into all of them.
    assert plain.chain.digests[1] != forked.chain.digests[1]

    openai_plain = _extract_openai(_openai_body(turns=4))
    openai_keyed = _extract_openai(_openai_body(turns=4, cache_key="bx1:deadbeef"))
    assert openai_plain.chain.digests[0] != openai_keyed.chain.digests[0]


def test_chain_kill_switch(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARM_CHAIN", "0")
    prefix = _extract_anthropic(_anthropic_body(turns=4))
    assert prefix is not None and prefix.chain is None


def test_chain_absent_without_exact_tokenizer(monkeypatch):
    """A heuristic tokenizer must produce NO chain rows, never mis-weighted ones."""
    monkeypatch.setattr(warming, "tokenizer_exact", lambda: False)
    prefix = _extract_anthropic(_anthropic_body(turns=4))
    assert prefix is not None and prefix.chain is None


def test_existing_prefix_hash_unchanged_by_chain_emission():
    """Golden value: the pre-change canonicalization, recomputed here from the
    payload the way warming.py has always done it. Chain emission must not move
    it by a byte -- every stored row, decision and reward join is keyed on it."""
    prefix = _extract_anthropic(_anthropic_body(turns=4))
    assert prefix.chain is not None          # the chain really was emitted
    canonical = json.dumps(prefix.payload, sort_keys=True,
                           separators=(",", ":"), default=str)
    assert prefix.prefix_hash == hashlib.sha256(
        canonical.encode("utf-8")).hexdigest()
    # Fixed fixture, fixed hash: recomputed on the pre-change extractor.
    fixed = extract_warm_prefix(
        {"model": MODEL,
         "system": [{"type": "text", "text": "s" * 40_000,
                     "cache_control": {"type": "ephemeral"}}],
         "messages": [{"role": "user", "content": "hello"}]},
        "anthropic", MODEL, {}, {"cache_control_owner": "caller"})
    assert fixed.prefix_hash == (
        "f928aeb172a717bae5a667d26b445c5d40f75de4c502434a867944e5d1c6b08f")


def test_chain_seed_records_the_scheme_and_ttl_tier():
    body = _anthropic_body(turns=4)
    body["messages"][-2]["content"][-1]["cache_control"] = {
        "type": "ephemeral", "ttl": "1h"}
    hourly = _extract_anthropic(body)
    default = _extract_anthropic(_anthropic_body(turns=4))
    assert hourly.chain.scheme == _CHAIN_SCHEME
    assert hourly.chain.digests[0] != default.chain.digests[0]


@pytest.mark.parametrize("provider,model,upstream", [
    ("openai", "gpt-5.6", "https://api.openai.com/v1/chat/completions"),
    ("deepseek", "deepseek-chat", "https://api.deepseek.com/chat/completions"),
])
def test_chain_emitted_for_automatic_providers(provider, model, upstream):
    body = _openai_body(turns=4)
    body["model"] = model
    prefix = extract_warm_prefix(body, provider, model, {}, {}, upstream=upstream)
    assert prefix is not None and prefix.chain is not None
    assert len(prefix.chain.digests) >= 2
    assert all(len(digest) == 64 for digest in prefix.chain.digests)


def test_chain_write_is_one_statement_not_one_subtransaction_per_block():
    """The tree lands in ONE insert, on both the SQL and the SQLite path.

    A PL/pgSQL block with an EXCEPTION clause always opens a subtransaction, and
    an insert-and-catch loop over the chain writes on every iteration, so it
    always takes an XID. A 300-block chain would assign 300+ subxids inside one
    transaction, overflowing PGPROC's 64-slot subxid cache and forcing
    pg_subtrans lookups for every concurrent backend on the instance -- on the
    live request path, for a best-effort analytics artifact.
    """
    migration = "supabase/migrations/202608100006_warm_prefix_chain_tree.sql"
    with open(migration, encoding="utf-8") as handle:
        text = handle.read()
    body = text.split("create or replace function public.warm_prefix_observe")[1]
    body = body.split("$$ language plpgsql")[0]
    assert body.count("insert into public.warm_prefix_node") == 1
    # No exception handler anywhere inside the per-depth loops: the only one in
    # the chain block is the outer "structure is best effort" handler.
    assert "exception when unique_violation" not in body
    assert body.count("exception when") == 1

    with open("api/store.py", encoding="utf-8") as handle:
        store_text = handle.read()
    mirror = store_text.split("def _warm_chain_write_locked")[1].split(
        "def _warm_customer_state_touch_locked")[0]
    assert mirror.count("INSERT INTO warm_prefix_node") == 1
    assert "executemany" in mirror
