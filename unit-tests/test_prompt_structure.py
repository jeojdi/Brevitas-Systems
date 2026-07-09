"""Structural parser: roles, protection, and exact reassembly."""

from token_efficiency_model.lossless.prompt_structure import (
    parse, high_value_tokens, TASK, CONTEXT, STYLE, CONSTRAINTS, EXAMPLES,
)

MARKETING = (
    "Write a marketing post about Brevitas.\n\n"
    "Audience: AI founders\nTone: technical\nPlatform: X\nLength: short\n\n"
    "Output JSON. Use snake_case. Never hallucinate. Cite sources.\n\n"
    "Context: Brevitas is a token-efficiency layer that sits in front of LLM providers. It "
    "compresses prompts losslessly where possible and reduces repeated context across turns, so "
    "teams cut their input-token spend without changing the model or the output they get back."
)


def test_reassembly_is_exact():
    segs = parse(MARKETING)
    assert "".join(s.text for s in segs) == MARKETING


def test_only_context_is_compressible():
    segs = parse(MARKETING)
    comp = [s for s in segs if s.compressible]
    assert len(comp) == 1
    assert comp[0].role == CONTEXT
    assert "token-efficiency layer" in comp[0].text


def test_task_line_is_protected():
    segs = parse(MARKETING)
    task = [s for s in segs if s.role == TASK]
    assert task and not task[0].compressible
    assert task[0].text.startswith("Write a marketing post")


def test_constraint_and_format_lines_protected():
    segs = parse(MARKETING)
    protected_text = " ".join(s.text for s in segs if not s.compressible)
    for directive in ["Output JSON", "snake_case", "Never hallucinate", "Cite sources"]:
        assert directive in protected_text


def test_labeled_style_block_protected():
    segs = parse(MARKETING)
    assert any(s.role == STYLE and not s.compressible for s in segs)


def test_fenced_code_is_examples_and_protected():
    p = "Do this:\n```python\ndef f():\n    return 1\n```\nThanks."
    segs = parse(p)
    ex = [s for s in segs if s.role == EXAMPLES]
    assert ex and not ex[0].compressible
    assert "def f():" in ex[0].text


def test_polite_instruction_is_task_not_context():
    p = "Please explain in detail why the sky appears blue during the day to a curious child."
    segs = parse(p)
    assert any(s.role == TASK and not s.compressible for s in segs)
    assert not any(s.compressible for s in segs)   # pure instruction -> nothing to compress


def test_high_value_tokens_include_numbers_and_entities():
    hv = high_value_tokens("Acme Corp must retain 4096 records for 90 days in snake_case format")
    assert "4096" in hv and "90" in hv and "Acme" in hv and "snake_case" in hv
