"""P0.3 + P1.10 policy regressions: safe request defaults and tighter cache eligibility."""
import os
import tempfile

os.environ.setdefault("BREVITAS_CACHE_DB", tempfile.mktemp(suffix=".db"))

import pytest

from brevitas.semantic_cache import SemanticCache


@pytest.fixture
def cache(tmp_path):
    return SemanticCache(str(tmp_path / "c.db"), semantic_enabled=False)


def _body(**extra):
    b = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    b.update(extra)
    return b


# ── P1.10: cache eligibility ─────────────────────────────────────────────────

def test_temperature_must_be_explicit_zero(cache):
    assert cache.cacheable(_body(temperature=0)) is True
    assert cache.cacheable(_body()) is False              # unset temperature
    assert cache.cacheable(_body(temperature=0.5)) is False
    assert cache.cacheable(_body(temperature=0.9)) is False


def test_multimodal_user_content_not_cacheable(cache):
    text_blocks = _body(temperature=0, messages=[
        {"role": "user", "content": [{"type": "text", "text": "hello"}]}])
    assert cache.cacheable(text_blocks) is True

    image = _body(temperature=0, messages=[
        {"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}])
    assert cache.cacheable(image) is False


def test_tools_and_stream_not_cacheable(cache):
    assert cache.cacheable(_body(temperature=0, tools=[{"type": "function"}])) is False
    assert cache.cacheable(_body(temperature=0, stream=True)) is False


def test_purge_removes_rows(cache):
    b = _body(temperature=0)
    cache.store(b, "openai", "gpt-4o-mini", {"x": 1}, prompt_tokens=1, completion_tokens=1)
    assert cache.lookup(b, "openai", "gpt-4o-mini") is not None
    removed = cache.purge()
    assert removed >= 1
    assert cache.lookup(b, "openai", "gpt-4o-mini") is None


def test_semantic_gate_honors_tenant_trip(tmp_path, monkeypatch):
    """A tenant-specific semantic trip must block only that tenant's fuzzy lookup.
    Exact byte-identical hits remain available because they are checked first."""
    import brevitas.semantic_cache as semantic_cache
    from token_efficiency_model.quality import gate

    monkeypatch.setenv("BREVITAS_SEMANTIC_CACHE", "1")
    monkeypatch.setattr(semantic_cache, "np", object())
    embed_calls = []
    monkeypatch.setattr(semantic_cache._embed, "embed",
                        lambda text: embed_calls.append(text) or None)

    c = SemanticCache(str(tmp_path / "tenant-gate.db"), semantic_enabled=False)
    body = _body(temperature=0)
    c.store(body, "openai", "gpt-4o-mini", {"answer": "exact"},
            prompt_tokens=1, completion_tokens=1)
    c.semantic_enabled = True
    gate.trip_lever("semantic_cache", key="tenant-a")
    try:
        assert c.lookup(body, "openai", "gpt-4o-mini", gate_key="tenant-a") is not None

        near = _body(temperature=0, messages=[{"role": "user", "content": "hello"}])
        assert c.lookup(near, "openai", "gpt-4o-mini", gate_key="tenant-a") is None
        assert embed_calls == []

        assert c.lookup(near, "openai", "gpt-4o-mini", gate_key="tenant-b") is None
        assert embed_calls == ["hello"]
    finally:
        gate.reset_all_levers(key="tenant-a")


# ── P0.3: safe defaults on the compress/playground request models ────────────

def test_compress_request_defaults_are_safe():
    from api.server import CompressRequest, PlaygroundChatRequest
    c = CompressRequest(messages=["hello"])
    assert c.lossy is False and c.retrieval is False
    p = PlaygroundChatRequest(messages=["hello"])
    assert p.lossy is False and p.retrieval is False


# ── B5: incomplete-response detection covers ALL choices ─────────────────────

def test_response_complete_requires_every_choice():
    from brevitas.proxy import _response_complete
    # OpenAI: every choice must be finish_reason == "stop"
    assert _response_complete({"choices": [{"finish_reason": "stop"}]}, "openai") is True
    assert _response_complete(
        {"choices": [{"finish_reason": "stop"}, {"finish_reason": "length"}]}, "openai") is False
    assert _response_complete({"choices": [{"finish_reason": "length"}]}, "openai") is False
    assert _response_complete({"choices": []}, "openai") is False
    # Anthropic: natural stop reasons only; a max_tokens truncation is not complete
    assert _response_complete({"stop_reason": "end_turn"}, "anthropic") is True
    assert _response_complete({"stop_reason": "max_tokens"}, "anthropic") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
