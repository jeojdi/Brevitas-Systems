"""Tests for the codebase scanner and codemod."""
from __future__ import annotations

import ast
import textwrap

from brevitas.scanner import rewrite_source, scan_source
from brevitas.scanner.codemod import _import_insert_line
from brevitas.scanner.models import Kind, Recommendation


def test_import_insert_line_never_above_shebang():
    src = "#!/usr/bin/env python3\nx = 1\n"
    assert _import_insert_line(ast.parse(src), src) == 2


def _clients(src):
    return [f for f in scan_source("x.py", textwrap.dedent(src)) if f.kind is Kind.CLIENT]


# ── detection ─────────────────────────────────────────────────────────────────

def test_detects_anthropic_module_construction():
    [c] = _clients("""
        import anthropic
        client = anthropic.Anthropic(api_key="sk-ant")
    """)
    assert c.provider == "anthropic"
    assert c.symbol == "anthropic.Anthropic"
    assert c.recommendation is Recommendation.APPLY


def test_detects_openai_from_import_and_alias():
    [c] = _clients("""
        from openai import OpenAI as LLM
        client = LLM(api_key="sk")
    """)
    assert c.provider == "openai"
    assert c.recommendation is Recommendation.APPLY


def test_async_client_is_manual_not_auto():
    [c] = _clients("""
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
    """)
    assert c.is_async is True
    assert c.recommendation is Recommendation.MANUAL


def test_already_wrapped_inline_is_done():
    [c] = _clients("""
        import anthropic, brevitas
        client = brevitas.wrap(anthropic.Anthropic())
    """)
    assert c.recommendation is Recommendation.DONE


def test_assigned_then_wrapped_is_done():
    # raw is constructed, then wrapped via a variable — must not be re-wrapped.
    [c] = _clients("""
        import anthropic, brevitas
        raw = anthropic.Anthropic()
        client = brevitas.wrap(raw)
    """)
    assert c.recommendation is Recommendation.DONE


def test_call_sites_and_pipeline_signal():
    from brevitas.scanner import scan_source as scan
    findings = scan("x.py", textwrap.dedent("""
        import anthropic
        c = anthropic.Anthropic()
        a = c.messages.create(model="m", messages=[])
        b = c.messages.create(model="m", messages=[])
    """))
    call_sites = [f for f in findings if f.kind is Kind.CALL_SITE]
    assert len(call_sites) == 2
    assert all(cs.var_name == "c" for cs in call_sites)


def test_ignores_unrelated_class_named_like_a_client():
    # `OpenAI` imported from another package is not the openai SDK.
    assert _clients("""
        from mylib import OpenAI
        x = OpenAI()
    """) == []


def test_recognises_real_openai_import():
    [c] = _clients("from openai import OpenAI\nx = OpenAI()")
    assert c.symbol == "OpenAI"
    assert c.provider == "openai"


def test_non_llm_messages_create_is_not_a_call_site():
    # A Twilio-style client with .messages.create must not be counted as a hop.
    findings = scan_source("x.py", textwrap.dedent("""
        notifier = SomethingElse()
        notifier.messages.create(to="+1234", body="hi")
    """))
    assert [f for f in findings if f.kind is Kind.CALL_SITE] == []


# ── codemod ───────────────────────────────────────────────────────────────────

def _rewrite(src):
    src = textwrap.dedent(src)
    findings = scan_source("x.py", src)
    return rewrite_source("x.py", src, findings)


def test_codemod_wraps_and_injects_import():
    change = _rewrite("""
        import anthropic
        client = anthropic.Anthropic(api_key="sk-ant")
    """)
    assert change is not None
    assert "brevitas.wrap(anthropic.Anthropic(api_key=\"sk-ant\"))" in change.modified
    assert "import brevitas" in change.modified
    # Output must still be valid Python.
    ast.parse(change.modified)


def test_codemod_is_idempotent():
    first = _rewrite("""
        import anthropic
        client = anthropic.Anthropic()
    """)
    second = rewrite_source("x.py", first.modified, scan_source("x.py", first.modified))
    assert second is None  # already wrapped -> no further change


def test_codemod_skips_async():
    change = _rewrite("""
        from openai import AsyncOpenAI
        client = AsyncOpenAI()
    """)
    assert change is None


def test_codemod_preserves_non_ascii_before_site():
    change = _rewrite("""
        import anthropic
        greeting = "café ☕ — résumé"
        client = anthropic.Anthropic()
    """)
    assert change is not None
    assert "café ☕ — résumé" in change.modified
    assert "brevitas.wrap(anthropic.Anthropic())" in change.modified
    ast.parse(change.modified)


def test_import_inserted_after_shebang():
    change = _rewrite("""\
        #!/usr/bin/env python3
        import anthropic
        client = anthropic.Anthropic()
    """)
    lines = change.modified.splitlines()
    assert lines[0] == "#!/usr/bin/env python3"  # shebang stays first
    assert "import brevitas" in change.modified
    ast.parse(change.modified)


def test_import_inserted_after_future():
    change = _rewrite("""
        from __future__ import annotations
        import anthropic
        client = anthropic.Anthropic()
    """)
    lines = change.modified.splitlines()
    future_idx = next(i for i, l in enumerate(lines) if "__future__" in l)
    brevitas_idx = next(i for i, l in enumerate(lines) if l.strip() == "import brevitas")
    assert brevitas_idx > future_idx
