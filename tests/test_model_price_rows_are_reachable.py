"""No MODEL_PRICES row may be unreachable, and a route may state its own price.

WHY THIS FILE EXISTS. `model_price` used to canonicalise the provider BEFORE
looking anything up, and `canonical_provider` collapses a model to the family
that originated it -- `claude*` becomes `anthropic` regardless of which route the
bytes actually took. So a row keyed ("bedrock", "claude-opus-5") could never be
returned: the lookup rewrote the key to ("anthropic", "claude-opus-5") first and
answered with the Anthropic first-party price.

That mattered because savings and the 25% fee derive entirely from this table.
Adding Bedrock, Vertex or Azure prices would have looked done, changed nothing,
and billed every cloud-routed request at the wrong provider's list price -- a
wrong NUMBER, silently, rather than an error.

Zero rows were unreachable when this was fixed, so the change was a provable
no-op at the time. These tests are what stop it becoming one later.
"""
from __future__ import annotations

import pytest

from brevitas.receipts import MODEL_PRICES, canonical_provider, model_price


def test_every_priced_row_is_reachable_by_its_own_key():
    unreachable = [
        (provider, model)
        for (provider, model) in MODEL_PRICES
        if model_price(provider, model) is not MODEL_PRICES[(provider, model)]
    ]
    assert not unreachable, (
        "these rows exist in MODEL_PRICES but model_price() answers with a "
        "DIFFERENT row, so they are dead code and the traffic they describe is "
        "billed at another provider's price:\n  "
        + "\n  ".join(f"{p}/{m}" for p, m in unreachable)
    )


#: The clouds, which canonical_provider DOES collapse: it matches on the model
#: name (`claude*` -> anthropic) before it ever looks at the provider argument.
#: Resellers are deliberately excluded here -- _RESELLER_PROVIDERS is checked
#: first in canonical_provider, so those routes already resolved to themselves
#: and were never affected by the dead-row bug. test_resellers_already_kept_
#: their_own_identity below pins that difference rather than hiding it.
@pytest.mark.parametrize("route", ["bedrock", "vertex", "azure_openai"])
def test_a_route_specific_row_wins_over_the_originating_family(route, monkeypatch):
    """The regression test. Without the route-first lookup this fails.

    Claude on Bedrock is not priced like Claude direct; pricing follows the bytes.
    A row keyed by the ROUTE must therefore beat the row keyed by the family that
    made the model.
    """
    model = "claude-opus-5"
    family = canonical_provider(route, model)
    assert family == "anthropic", (
        "canonical_provider no longer collapses claude* to anthropic; this test "
        "was written against that behaviour and should be re-derived"
    )

    route_price = {"input": 1.0, "cached": 0.1, "write": 1.25, "output": 2.0}
    patched = dict(MODEL_PRICES)
    patched[(route, model)] = route_price
    monkeypatch.setattr("brevitas.receipts.MODEL_PRICES", patched)

    resolved = model_price(route, model)
    assert resolved is route_price, (
        f"model_price({route!r}, {model!r}) returned the {family} price instead "
        f"of the route's own. Route-specific rows are unreachable, so adding "
        f"{route} prices would be dead code."
    )
    # And the family row still answers for the family itself.
    assert model_price(family, model) is patched[(family, model)]


@pytest.mark.parametrize("reseller", ["openrouter", "together", "fireworks", "groq"])
def test_resellers_already_kept_their_own_identity(reseller):
    """Resellers were never subject to the dead-row bug, and this says why.

    canonical_provider checks _RESELLER_PROVIDERS FIRST, so a reseller route
    resolves to itself even for a claude* model. The clouds fall through to the
    model-name rules instead, which is exactly what made their rows unreachable.
    Pinning the asymmetry stops someone "simplifying" the two branches together.
    """
    assert canonical_provider(reseller, "claude-opus-5") == reseller


def test_an_unpriced_route_still_falls_back_to_the_family():
    """The fallback must survive: most routes will never publish their own table.

    A reseller proxying Claude with no route-specific row should still price at
    the Anthropic table rather than returning None, which would make savings $0.
    """
    assert model_price("some-unlisted-gateway", "claude-opus-5") == \
        MODEL_PRICES[("anthropic", "claude-opus-5")]


def test_dated_snapshots_still_resolve_through_the_alias():
    """Longest-prefix snapshot matching is unchanged by the route-first lookup."""
    assert model_price("anthropic", "claude-haiku-4-5-20251001") == \
        MODEL_PRICES[("anthropic", "claude-haiku-4-5-20251001")]
    # A snapshot with no exact row falls back to its alias, longest match first.
    assert model_price("openai", "gpt-4.1-mini-2025-04-14") is not None
