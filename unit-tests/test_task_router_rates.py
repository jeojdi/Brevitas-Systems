"""Task classification -> rate table, and the lightened force-list for non-code tasks."""

import pytest

from token_efficiency_model.lossless.task_router import (
    classify_task,
    _protect_tokens,
    _HEAVY_PROTECT_TASKS,
    _DEFAULT_RATES,
)


@pytest.mark.parametrize("prompt,expected", [
    ("Build me a React component with a login form", "code"),
    ("Write a catchy Instagram caption for our new sneaker", "creative"),
    ("Summarize this article in three bullet points", "summarize"),
    ("Extract the exact invoice total from this text", "extraction"),
    ("Calculate the derivative and prove the result step by step", "reasoning"),
    ("Tell me about the history of jazz", "general"),
])
def test_classify_task(prompt, expected):
    assert classify_task(prompt) == expected


def test_hint_overrides_classification():
    assert classify_task("anything at all", hint="reasoning") == "reasoning"
    assert classify_task("anything at all", hint="not-a-real-task") == "general"


def test_rate_table_is_conservative_for_precise_tasks():
    assert _DEFAULT_RATES["extraction"] >= _DEFAULT_RATES["creative"]
    assert _DEFAULT_RATES["reasoning"] >= _DEFAULT_RATES["general"]


def test_heavy_protection_only_for_precise_tasks():
    text = "charge the account 4096 credits for order 12345 and refund 50 on cancellation"
    heavy = _protect_tokens(text, "code")
    light = _protect_tokens(text, "creative")
    # precise tasks force load-bearing NUMBERS; creative keeps only structural punctuation.
    # (Identifiers are NOT forced — large force lists crash real LLMLingua-2.)
    assert "4096" in heavy and "12345" in heavy and "50" in heavy
    assert "4096" not in light and "12345" not in light
    assert set(light).issubset(set(heavy))
    assert "creative" not in _HEAVY_PROTECT_TASKS


def test_identifiers_are_not_forced():
    # prose identifiers must NOT be stuffed into force_tokens (they crash LLMLingua-2)
    text = "call computeTotalAmount on the orderService before returning the response payload"
    heavy = _protect_tokens(text, "code")
    assert "computeTotalAmount" not in heavy and "orderService" not in heavy
