"""Entity-corruption coverage for the lossy compression path.

The information-density gate treats only ``numbers``, ``constraints``, ``formatting``
and ``task`` as CRITICAL (``quality_metrics._CRITICAL``). ``entities`` is measured and
reported but is NOT part of ``overall_ok``, so a compression that silently drops a
person's name, a product name, an order id or a code identifier can still be accepted
and shipped to the provider.

These tests pin that behaviour down with the REAL LLMLingua-2 model rather than a fake,
across the four entity classes a customer would care about:

  * proper names / organizations   (Priya Raghavan, Northwind Trading, Contoso)
  * numbers, amounts, dates        (already gated — regression guard)
  * opaque identifiers             (order ids, SKUs, UUIDs, git shas)
  * code identifiers               (snake_case, CamelCase, dotted attribute paths)

`test_entity_survival_report` is the measurement: it prints a per-class survival table
and asserts only the contract the code actually makes today (numbers survive). The
remaining tests assert the specific gaps, so if the gate is later tightened to cover
entities these tests fail loudly and get updated rather than silently passing.

Requires the `llmlingua` package (pip install "brevitas-systems[promptopt]"); skipped
otherwise, like the other real-model tests in this directory.
"""

import re

import pytest

from token_efficiency_model.lossless.message_optimizer import optimize_message_text
from token_efficiency_model.lossless.quality_metrics import information_density

# ---------------------------------------------------------------------------
# Fixtures: each prompt has a labelled Context block (the only compressible part)
# carrying entities of one class, plus enough prose for the ladder to bite.
# ---------------------------------------------------------------------------

_PAD = (
    "The team reviewed the material at length and generally agreed that the overall "
    "direction was sound, although several members noted that the supporting detail "
    "could have been organised more clearly for a reader coming to it fresh. "
)

NAMES = ["Priya Raghavan", "Northwind Trading", "Contoso", "Aurelio Marchetti"]
NAME_PROMPT = (
    "Summarize the account review.\n\n"
    "Context: The renewal negotiation for Northwind Trading was led by Priya Raghavan, "
    "who reported that Contoso remains the incumbent vendor on the data platform and "
    "that Aurelio Marchetti will take over the relationship in the next quarter. "
    + _PAD * 3 +
    "\n\nConstraints: three bullets.\n"
)

NUMBERS = ["4281930", "11.4", "2026-04-18", "229"]
NUMBER_PROMPT = (
    "Summarize the quarter.\n\n"
    "Context: Total bookings reached 4281930 dollars, an increase of 11.4 percent over "
    "the prior quarter, and the board reviewed the figures on 2026-04-18 when headcount "
    "stood at 229 people across the combined organisation. "
    + _PAD * 3 +
    "\n\nConstraints: three bullets.\n"
)

IDENTIFIERS = ["ORD-99312-B", "SKU-77410", "9f3c1ab", "b7e1d4c2-51aa-4f0e-9c33-2d5b81a77e10"]
IDENTIFIER_PROMPT = (
    "Summarize the incident.\n\n"
    "Context: The failing shipment was recorded under order ORD-99312-B against part "
    "SKU-77410, and the regression was introduced by commit 9f3c1ab which the on-call "
    "engineer correlated with trace b7e1d4c2-51aa-4f0e-9c33-2d5b81a77e10 in the logs. "
    + _PAD * 3 +
    "\n\nConstraints: three bullets.\n"
)

CODE_IDENTS = ["retry_backoff_seconds", "PaymentIntentHandler", "billing.ledger.settle_period"]
CODE_PROMPT = (
    "Summarize the change.\n\n"
    "Context: The patch lowers retry_backoff_seconds so that the PaymentIntentHandler "
    "retries sooner, and it moves the settlement call out of the request path into "
    "billing.ledger.settle_period where it can be batched with the nightly run. "
    + _PAD * 3 +
    "\n\nConstraints: three bullets.\n"
)

CASES = [
    ("names", NAME_PROMPT, NAMES),
    ("numbers", NUMBER_PROMPT, NUMBERS),
    ("identifiers", IDENTIFIER_PROMPT, IDENTIFIERS),
    ("code_identifiers", CODE_PROMPT, CODE_IDENTS),
]


def _survivors(original: str, entities: list[str]) -> tuple[dict, dict]:
    """Compress `original` and report which `entities` survived verbatim."""
    out = optimize_message_text(original)
    kept = {e: (e in out["text"]) for e in entities}
    return out, kept


@pytest.mark.xfail(
    strict=False,
    reason="KNOWN DEFECT: LLMLingua-2 re-spaces punctuation, so hyphenated entities "
           "(ISO dates, order ids, SKUs, UUIDs, dotted attribute paths) come back split "
           "-- '2026-04-18' -> '2026 - 04 - 18'. information_density does not catch it "
           "because _NUM/_ENTITY/_IDENT tokenize the entity into fragments and _ratio "
           "does a SUBSTRING test, so every fragment is still 'present' and the gate "
           "scores 1.0 on all critical classes. Remove this xfail once the gate compares "
           "whole entities (see test_hyphenated_entities_are_split_by_the_model).",
)
@pytest.mark.parametrize("label,prompt,entities", CASES, ids=[c[0] for c in CASES])
def test_entities_survive_compression_verbatim(local_llmlingua_remote, label, prompt,
                                               entities, capsys):
    """Every load-bearing entity must appear byte-identical in an accepted compression."""
    out, kept = _survivors(prompt, entities)
    lost = [e for e, ok in kept.items() if not ok]

    with capsys.disabled():
        print(f"\n[{label}] reason={out['reason']} "
              f"{out['tokens_before']}->{out['tokens_after']} tok "
              f"({100 * (1 - out['tokens_after'] / max(1, out['tokens_before'])):.1f}% saved)")
        for entity, ok in kept.items():
            print(f"    {'KEPT ' if ok else 'LOST '} {entity!r}")
        if out["info_density"]:
            density = out["info_density"]
            print(f"    density: numbers={density['numbers']} entities={density['entities']} "
                  f"constraints={density['constraints']} overall_ok={density['overall_ok']}")

    if out["reason"] != "compressed":
        pytest.skip(f"compression not applied ({out['reason']}); nothing to corrupt")

    assert not lost, f"accepted compression corrupted {label}: {lost}"


def test_hyphenated_entities_are_split_by_the_model(local_llmlingua_remote):
    """Root cause, pinned separately so it is diagnosable without the parametrized suite.

    The gate reports full retention while the entity is materially destroyed.
    """
    out = optimize_message_text(IDENTIFIER_PROMPT)
    if out["reason"] != "compressed":
        pytest.skip(f"compression not applied ({out['reason']})")

    text = out["text"]
    split_forms = ["ORD - 99312 - B", "SKU - 77410"]
    observed = [s for s in split_forms if s in text]
    assert observed, f"expected re-spaced identifiers, got: {text[:400]!r}"

    # ...and the gate still calls this a perfect compression.
    density = information_density(IDENTIFIER_PROMPT, text)
    assert density["overall_ok"] is True
    assert density["numbers"] == 1.0, (
        "numbers scored < 1.0 -- the substring loophole may have been closed; "
        "re-check whether this xfail suite can be tightened")


def test_task_and_constraints_stay_byte_identical(local_llmlingua_remote):
    """Non-context segments must never be rewritten, whatever happens to Context."""
    for _, prompt, _ in CASES:
        out = optimize_message_text(prompt)
        assert prompt.split("\n\n")[0] in out["text"]
        assert "Constraints: three bullets." in out["text"]


def test_entities_are_not_a_critical_class():
    """Pins the actual contract: entity retention is reported but never enforced.

    If this fails, `_CRITICAL` was widened to include entities — good news, but the
    survival tests above must then be tightened from measurement to assertion.
    """
    original = "Priya Raghavan approved the Northwind renewal within 30 days as required."
    # entities gone, numbers + constraint kept
    compressed = "approved the renewal within 30 days as required."
    density = information_density(original, compressed)
    assert density["entities"] < 1.0
    assert density["numbers"] == 1.0
    assert density["overall_ok"] is True, (
        "entities now affect overall_ok — tighten the survival tests above")


def test_force_token_budget_is_capped_below_entity_count():
    """`high_value_tokens` caps protection at 80 items, so long contexts are unprotected.

    A 100K-token document routinely contains thousands of distinct entities; only the
    first 80 distinct numbers/identifiers/entities are ever passed to the model as
    force_tokens, and the rest have no protection at all.
    """
    from token_efficiency_model.lossless.prompt_structure import high_value_tokens

    many = " ".join(f"Entity{i} ident_{i} {1000 + i}" for i in range(500))
    forced = high_value_tokens(many)
    assert len(forced) == 80
    distinct = len(set(re.findall(r"\bEntity\d+\b|\bident_\d+\b|\b\d{4}\b", many)))
    assert distinct > 1000
    assert len(forced) < distinct
