"""Bedrock model ids must map onto priced models, or refuse.

The failure this guards is silent: an id that normalises to the WRONG model
prices the request at another model's rate, and the fee is a percentage of a
number derived from that rate. An id that refuses merely bills $0. So every test
here is written to prefer refusal over a guess.
"""
from __future__ import annotations

import pytest

from brevitas.bedrock import bedrock_provider_label, normalize_bedrock_model
from brevitas.receipts import MODEL_PRICES, calculate_costs, TokenReceipt


@pytest.mark.parametrize(("model_id", "expected"), [
    # Bare vendor-namespaced ids.
    ("anthropic.claude-opus-5-v1:0", "claude-opus-5"),
    ("anthropic.claude-sonnet-5-v1:0", "claude-sonnet-5"),
    ("anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5-20251001"),
    # us. and global. inference profiles bill at parity, so they normalise.
    ("us.anthropic.claude-opus-5-v1:0", "claude-opus-5"),
    ("global.anthropic.claude-sonnet-5-v1:0", "claude-sonnet-5"),
    # Version suffix variants.
    ("anthropic.claude-opus-5-v2:1", "claude-opus-5"),
    ("anthropic.claude-opus-5:0", "claude-opus-5"),
    # Case and whitespace.
    ("US.Anthropic.Claude-Opus-5-v1:0", "claude-opus-5"),
    ("  us.anthropic.claude-opus-5-v1:0  ", "claude-opus-5"),
])
def test_recognised_ids_normalise_to_the_priced_alias(model_id, expected):
    assert normalize_bedrock_model(model_id) == expected


@pytest.mark.parametrize("model_id", [
    # Cross-region profiles bill ABOVE parity. Normalising them would understate
    # actual spend, overstate savings, and inflate the invoice.
    "eu.anthropic.claude-opus-5-v1:0",
    "apac.anthropic.claude-sonnet-5-v1:0",
    "us-gov.anthropic.claude-opus-5-v1:0",
    # No vendor namespace: not a Bedrock id. Refusing stops a caller smuggling an
    # arbitrary model name into the Bedrock lane to have it priced.
    "claude-opus-5",
    "gpt-4o",
    # Empty and junk.
    "", "   ", ":", "-v1:0",
])
def test_unverified_ids_refuse_rather_than_guess(model_id):
    assert normalize_bedrock_model(model_id) == "", (
        f"{model_id!r} normalised to something instead of refusing. An id we have "
        "not verified pricing parity for must fall into the unpriced path, where "
        "it bills $0, rather than being priced as a model it may not be."
    )


def test_every_normalised_alias_is_actually_priced():
    """A mapping that produces an unpriced alias is a mapping that does nothing.

    This is the join between this module and MODEL_PRICES: if a Claude alias is
    renamed in the price table, the ids here must stop resolving to it, and this
    test is what says so.
    """
    ids = [
        "anthropic.claude-opus-5-v1:0",
        "us.anthropic.claude-sonnet-5-v1:0",
        "anthropic.claude-haiku-4-5-v1:0",
    ]
    unpriced = [
        model_id for model_id in ids
        if ("anthropic", normalize_bedrock_model(model_id)) not in MODEL_PRICES
    ]
    assert not unpriced, (
        "these Bedrock ids normalise to aliases that MODEL_PRICES does not "
        f"contain, so they price at $0: {unpriced}"
    )


def test_the_provider_label_stays_bedrock():
    """Pricing follows the bytes, and the bytes went to Bedrock.

    Recording "anthropic" would erase the route and make a future
    ("bedrock", model) price row unreachable -- the exact defect that was fixed
    in model_price and calculate_costs.
    """
    assert bedrock_provider_label("us.anthropic.claude-opus-5-v1:0") == "bedrock"


def test_a_bedrock_receipt_prices_through_the_route_and_falls_back_today():
    """End to end: normalise, then price under the bedrock label.

    With no ("bedrock", …) rows in the table today this must fall back to the
    Anthropic price -- Bedrock Claude is at on-demand parity, so that is correct
    AND it means shipping the lane needs no price rows at all. The moment a
    ("bedrock", …) row is added it wins, because model_price resolves the route
    first.
    """
    alias = normalize_bedrock_model("us.anthropic.claude-opus-5-v1:0")
    receipt = TokenReceipt(
        fresh_input_tokens=1_000_000, cached_input_tokens=0, output_tokens=1_000_000,
    )
    costs = calculate_costs(bedrock_provider_label(""), alias, 1_000_000, receipt)

    assert costs["pricing_status"] == "priced"
    assert costs["prices"] == MODEL_PRICES[("anthropic", "claude-opus-5")]


def test_a_refused_id_produces_no_price_at_all():
    """The safe failure: unpriced, so settlement evidence excludes the row."""
    alias = normalize_bedrock_model("eu.anthropic.claude-opus-5-v1:0")
    assert alias == ""
    receipt = TokenReceipt(
        fresh_input_tokens=1_000, cached_input_tokens=0, output_tokens=1_000,
    )
    costs = calculate_costs("bedrock", alias, 1_000, receipt)
    assert costs["pricing_status"] == "unpriced"
    assert costs["measured_savings_usd"] is None
