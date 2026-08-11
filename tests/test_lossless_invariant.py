"""Killswitch guard for the one invariant the 25% fee rests on.

Brevitas bills 25% of RECEIPT-VERIFIED savings, and the whole product claim is
that those savings cost the customer nothing they can observe: the bytes the
model returned are the bytes they would have got anyway. api/server.py encodes
that claim in two tuples and one function:

    api.server._QUALITY_AFFECTING_STRATEGIES
    api.server._BYTE_PRESERVING_STRATEGIES
    api.server._verification_mode()

and the money gate downstream, in api.server._record_usage_report, is exactly

    verified = measured if authoritative and mode == "byte_preserving" ... else 0

so a strategy's tuple membership IS its billability. Moving the single string
"semantic_cache" from the first tuple to the second is a one-line diff that
reads like tidying and is not: it makes cosine similarity a billing predicate.
A nearest-neighbour hit is not a proof of answer equivalence — it is a guess
that two prompts meant the same thing — and the instant one funds an invoice,
"lossless" is a marketing word rather than a property, every fee row minted
under it is unearned, and there is no receipt that can retroactively prove the
customer got the answer they paid for.

That edit had exactly one incidental defender
(tests/test_cloud_usage_api.py::test_authoritative_billing_bills_exact_replays_not_fuzzy_reuse_or_unknown_models),
which is a billing-arithmetic test that happens to include a fuzzy row. This
file is the dedicated one. It exists so that a reviewer who moves a string
between those tuples, adds a name to the byte-preserving side, or reorders
_verification_mode so the cache_attributable fallback runs first, gets a test
failure in their face that names the invariant instead of a rounding delta.

Note on _verification_mode's ORDER, which several tests below pin: the
quality-affecting check runs BEFORE the `cache_attributable` fallback. That
ordering is load-bearing, not incidental. `cache_attributable` means "a
provider-native cache discount showed up on this receipt" (DeepSeek's
automatic 128-token-block prefix cache does this with no Brevitas involvement
at all — see docs/DEEPSEEK_CACHE_MAP.md). A row can be BOTH natively cached and
fuzzily reused; if the fallback won, any quality-affecting strategy could be
laundered into a billable one just by riding on a provider discount Brevitas
did not cause. Quality-affecting must always win.

Additions to the quality-affecting tuple are safe (they only ever narrow what
bills). Additions to the byte-preserving tuple are the dangerous direction, so
that tuple is pinned by exact membership below: widening it requires a
deliberate edit here plus the byte-equality argument that justifies it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import api.server as server
from api.auth import hash_key
from api.store import UsageStore

# Every name that is fuzzy, lossy, or otherwise rewrites what the model sees.
# None of these can ever prove the customer's bytes were preserved, so each one
# must be quality-affecting AND absent from the byte-preserving tuple. Asserted
# in BOTH directions on purpose: a future edit could ADD a name to the
# byte-preserving side without removing it from this one, and under a reordered
# _verification_mode that would be enough to make it bill.
FUZZY_STRATEGIES = (
    "semantic_cache",   # cosine similarity, not equivalence — the whole point
    "response_cache",   # reuses a response for a request that only looks alike
    "reorder",          # changes prompt ordering, so changes the sampled answer
    "compress",         # drops tokens the caller wrote
    "retrieve",         # substitutes retrieved context for the caller's
    "retrieval",
    "llmlingua",        # prompt compression; lossy by construction
    "lossy",            # says so on the tin
)

# Exact membership, not a subset. Each of these is byte-preserving because
# nothing about the request the provider answered changed: a native/provider
# cache read (native_cache, cache_only), an untouched passthrough, or an
# exact-hash replay of a byte-identical request (exact_cache). Adding a name
# here widens what Brevitas is allowed to charge for, so it must break this
# test and be argued for in the diff that adds it.
BYTE_PRESERVING_MEMBERS = frozenset({
    "native_cache", "cache_only", "passthrough", "byte_preserving",
    "lossless", "exact_cache",
})


def test_semantic_cache_is_quality_affecting_and_never_byte_preserving():
    """The single string whose relocation would breach the invariant."""
    assert "semantic_cache" in server._QUALITY_AFFECTING_STRATEGIES, (
        "semantic_cache was removed from _QUALITY_AFFECTING_STRATEGIES. Fuzzy "
        "reuse is billable the moment it stops being classified quality-affecting."
    )
    assert "semantic_cache" not in server._BYTE_PRESERVING_STRATEGIES, (
        "semantic_cache was added to _BYTE_PRESERVING_STRATEGIES. Cosine "
        "similarity is not proof of answer equivalence and must never fund a fee."
    )


@pytest.mark.parametrize("strategy", FUZZY_STRATEGIES)
def test_fuzzy_strategies_stay_quality_affecting_and_out_of_byte_preserving(strategy):
    assert strategy in server._QUALITY_AFFECTING_STRATEGIES, (
        f"{strategy!r} lost its quality-affecting classification; savings "
        f"reported under it would become billable."
    )
    assert strategy not in server._BYTE_PRESERVING_STRATEGIES, (
        f"{strategy!r} was added to _BYTE_PRESERVING_STRATEGIES."
    )


@pytest.mark.parametrize("strategy", FUZZY_STRATEGIES)
def test_fuzzy_strategies_classify_quality_affecting_even_when_cache_attributable(strategy):
    """Quality-affecting must beat the cache_attributable fallback.

    _verification_mode returns "byte_preserving" for any cache_attributable row
    whose label matches neither tuple. A provider-native discount is not a
    licence to bill a fuzzy reuse, so the quality-affecting branch has to run
    first. This is the test that fails if the branches are ever swapped.
    """
    assert server._verification_mode(strategy) == "quality_affecting"
    assert server._verification_mode(
        strategy, cache_attributable=True) == "quality_affecting"


@pytest.mark.parametrize("label", [
    "semantic_cache",
    "semantic_cache_v2",          # matching is substring-based, so suffixes match
    "  SEMANTIC_CACHE  ",         # _verification_mode strips and lowercases
    "semantic_cache_lossless",    # cannot borrow billability from a lossless marker
    "hybrid_semantic_cache",
])
def test_semantic_cache_labels_cannot_be_relabelled_into_billability(label):
    """A caller-chosen strategy label must not be a route around the invariant.

    `strategy` is customer-supplied metadata (UsageReportRequest), so the label
    space is adversarial. Substring matching plus quality-affecting-first means
    any label CONTAINING a fuzzy marker classifies quality-affecting no matter
    what else it contains — including a lossless-sounding suffix.
    """
    assert server._verification_mode(label) == "quality_affecting"
    assert server._verification_mode(
        label, cache_attributable=True) == "quality_affecting"


def test_strategy_tuples_are_disjoint():
    """No name may sit in both tuples.

    A name in both is not merely redundant: it means the two tuples disagree
    about whether that strategy is billable, and which answer wins is then an
    accident of the branch order inside _verification_mode.
    """
    overlap = (set(server._QUALITY_AFFECTING_STRATEGIES)
               & set(server._BYTE_PRESERVING_STRATEGIES))
    assert overlap == set(), (
        f"strategies classified both quality-affecting and byte-preserving: "
        f"{sorted(overlap)}"
    )


def test_no_marker_is_a_substring_of_a_marker_on_the_other_side():
    """Disjointness is not enough — matching is `in`, not `==`.

    _verification_mode tests `marker in value`, so a broad marker on one side
    can swallow a specific name on the other (adding "cache" to the
    byte-preserving tuple would make it match "semantic_cache"). Today the
    branch order still saves us, but that is one reordering away from being
    load-bearing twice. Keep the two vocabularies non-overlapping as SUBSTRINGS
    so the invariant does not depend on branch order alone.
    """
    collisions = [
        (quality, byte_preserving)
        for quality in server._QUALITY_AFFECTING_STRATEGIES
        for byte_preserving in server._BYTE_PRESERVING_STRATEGIES
        if quality in byte_preserving or byte_preserving in quality
    ]
    assert collisions == [], (
        f"substring collisions across the classification tuples: {collisions}"
    )


def test_byte_preserving_membership_is_pinned():
    """Exact membership. Widening this set widens what Brevitas can charge for.

    Deliberately not a subset assertion: a new name here is a claim that some
    new code path leaves the response bytes provably unchanged, and that claim
    belongs in a diff that also edits this list.
    """
    assert set(server._BYTE_PRESERVING_STRATEGIES) == BYTE_PRESERVING_MEMBERS, (
        "_BYTE_PRESERVING_STRATEGIES changed. Every name in it is billable by "
        "construction; add one only with a byte-equality argument, then update "
        "BYTE_PRESERVING_MEMBERS here."
    )


@pytest.mark.parametrize("strategy", sorted(BYTE_PRESERVING_MEMBERS))
def test_byte_preserving_strategies_still_classify_byte_preserving(strategy):
    """Guards against a vacuous pass of the fuzzy tests above.

    If some edit made _verification_mode return "quality_affecting" for
    everything, every assertion in this file about fuzzy names would still
    pass while billing quietly went to zero. This is the other side of that.
    """
    assert server._verification_mode(strategy) == "byte_preserving"


@pytest.mark.parametrize("strategy", server._BREVITAS_REPLAY_STRATEGIES)
def test_replay_strategies_are_classified_by_the_tuples_not_the_fallback(strategy):
    """Replay rows are zero-spend by construction, so classification is all there is.

    api.server._BREVITAS_REPLAY_STRATEGIES are the strategies whose
    upstream cost is zero because no upstream call happened. That makes their
    entire reported saving a function of which tuple the label lands in, with
    nothing else to cross-check it against — so an "unknown" classification
    here would mean a money-bearing label nobody has ruled on.
    """
    assert server._verification_mode(strategy) in ("quality_affecting", "byte_preserving")


@pytest.mark.parametrize("strategy", [
    s for s in server._BREVITAS_REPLAY_STRATEGIES
    if s in server._QUALITY_AFFECTING_STRATEGIES
])
def test_quality_affecting_replay_strategies_are_never_byte_preserving(strategy):
    """A quality-affecting replay (semantic_cache today) must stay unbillable.

    This is the most dangerous corner in the classifier: a replay strategy
    reports zero upstream spend, so if it ALSO classified byte-preserving its
    full baseline would land in verified_savings_usd with no provider receipt
    contradicting it.
    """
    assert strategy not in server._BYTE_PRESERVING_STRATEGIES
    assert server._verification_mode(strategy) == "quality_affecting"
    assert server._verification_mode(
        strategy, cache_attributable=True) == "quality_affecting"


def test_authoritative_fuzzy_reuse_bills_nothing_even_when_cache_attributable(
        tmp_path, monkeypatch):
    """The end-to-end money assertion, not just the classifier's return value.

    Every condition here is the FAVOURABLE one for billing — authoritative
    (the in-process proxy observed the provider response), quality_verified
    True, and cache_attributable True so the fallback would fire if it were
    reached. The exact_cache row is the positive control: it proves the
    fixture actually prices and bills, so the zero on the semantic_cache row
    is the invariant holding rather than the harness being inert.
    """
    store = UsageStore(str(tmp_path / "lossless-invariant.db"))
    store.create_key(hash_key("bvt_lossless_invariant"), "lossless-invariant")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()

    def report(strategy, request_id):
        return server._record_usage_report(
            hash_key("bvt_lossless_invariant"),
            server.UsageReportRequest(
                provider="openai", model="gpt-4o-mini",
                baseline_tokens=100, compressed_tokens=0,
                fresh_input_tokens=0, output_tokens=0,
                baseline_output_tokens=20, strategy=strategy,
                quality_verified=True, cache_attributable=True,
                request_id=request_id,
            ),
            authoritative=True,
        )

    exact_replay = report("exact_cache", "lossless-invariant-exact")
    fuzzy_reuse = report("semantic_cache", "lossless-invariant-semantic")

    # Positive control: a byte-identical replay is billable, so the fixture is live.
    assert exact_replay["verified_savings_usd"] > 0
    assert exact_replay["brevitas_fee_usd"] > 0
    # The invariant: fuzzy reuse creates no verified savings and no fee, under
    # the most billing-favourable inputs the intake path accepts.
    assert fuzzy_reuse["verified_savings_usd"] == 0
    assert fuzzy_reuse["brevitas_fee_usd"] == 0
