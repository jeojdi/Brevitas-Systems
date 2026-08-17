"""Unit tests for brevitas.hosted() — the one-call hosted-gateway configurator.

Uses SimpleNamespace stand-ins for the OpenAI/Anthropic SDK clients (same approach as
tests/test_receipt_alignment_wrappers.py) so the real provider SDKs are not required.
"""
from types import SimpleNamespace

import pytest

import brevitas


def _openai_like():
    """An OpenAI-style client: has .chat.completions and .with_options."""
    captured = {}

    def with_options(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(configured=True, **kwargs)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_k: None)),
        with_options=with_options,
    )
    return client, captured


def _anthropic_like():
    """An Anthropic-style client: has .messages.create and .copy (no with_options)."""
    captured = {}

    def copy(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(configured=True, **kwargs)

    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_k: None),
        copy=copy,
    )
    return client, captured


def test_hosted_sets_gateway_and_both_headers(monkeypatch):
    monkeypatch.delenv("BREVITAS_BASE_URL", raising=False)
    client, captured = _openai_like()
    result = brevitas.hosted(client, api_key="bvt_key", customer_id="acme")
    assert captured["base_url"] == "https://api.brevitassystems.com/v1"
    assert captured["default_headers"] == {
        "X-Brevitas-Key": "bvt_key",
        "X-Brevitas-Customer-ID": "acme",
    }
    assert result.configured is True


def test_hosted_reads_env_fallbacks(monkeypatch):
    monkeypatch.setenv("BREVITAS_API_KEY", "bvt_env")
    monkeypatch.setenv("BREVITAS_CUSTOMER_ID", "envco")
    monkeypatch.delenv("BREVITAS_BASE_URL", raising=False)
    client, captured = _openai_like()
    brevitas.hosted(client)
    assert captured["default_headers"] == {
        "X-Brevitas-Key": "bvt_env",
        "X-Brevitas-Customer-ID": "envco",
    }


def test_hosted_explicit_args_override_env(monkeypatch):
    monkeypatch.setenv("BREVITAS_API_KEY", "env")
    monkeypatch.setenv("BREVITAS_CUSTOMER_ID", "envco")
    monkeypatch.delenv("BREVITAS_BASE_URL", raising=False)
    client, captured = _openai_like()
    brevitas.hosted(client, api_key="explicit", customer_id="explicitco")
    assert captured["default_headers"]["X-Brevitas-Key"] == "explicit"
    assert captured["default_headers"]["X-Brevitas-Customer-ID"] == "explicitco"


def test_hosted_requires_a_brevitas_key(monkeypatch):
    monkeypatch.delenv("BREVITAS_API_KEY", raising=False)
    client, _ = _openai_like()
    with pytest.raises(ValueError):
        brevitas.hosted(client)


def test_hosted_omits_customer_header_when_unresolved(monkeypatch):
    monkeypatch.delenv("BREVITAS_CUSTOMER_ID", raising=False)
    monkeypatch.delenv("BREVITAS_BASE_URL", raising=False)
    client, captured = _openai_like()
    brevitas.hosted(client, api_key="bvt_key")
    assert captured["default_headers"] == {"X-Brevitas-Key": "bvt_key"}
    assert "X-Brevitas-Customer-ID" not in captured["default_headers"]


def test_hosted_base_url_override_appends_v1_exactly_once():
    client, captured = _openai_like()
    brevitas.hosted(client, api_key="k", base_url="http://localhost:8000")
    assert captured["base_url"] == "http://localhost:8000/v1"

    client_v1, captured_v1 = _openai_like()
    brevitas.hosted(client_v1, api_key="k", base_url="http://localhost:8000/v1")
    assert captured_v1["base_url"] == "http://localhost:8000/v1"


def test_hosted_supports_anthropic_like_client_via_copy(monkeypatch):
    monkeypatch.delenv("BREVITAS_BASE_URL", raising=False)
    client, captured = _anthropic_like()
    brevitas.hosted(client, api_key="k", customer_id="c")
    assert captured["base_url"] == "https://api.brevitassystems.com/v1"
    assert captured["default_headers"]["X-Brevitas-Key"] == "k"
    assert captured["default_headers"]["X-Brevitas-Customer-ID"] == "c"


def test_hosted_rejects_a_non_sdk_object():
    # Has with_options but is not an OpenAI/Anthropic client.
    with pytest.raises(TypeError):
        brevitas.hosted(SimpleNamespace(with_options=lambda **_k: None), api_key="k")


def test_hosted_header_names_match_the_cli_snippets():
    # The wrapper and the connect-CLI snippets must emit identical header names.
    from brevitas import identity
    from brevitas import cli
    assert cli.BREVITAS_KEY_HEADER == identity.KEY_HEADER == "X-Brevitas-Key"
    assert cli.CUSTOMER_ID_HEADER == identity.CUSTOMER_HEADER == "X-Brevitas-Customer-ID"
