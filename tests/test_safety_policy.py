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


# ── P0.3: safe defaults on the compress/playground request models ────────────

def test_compress_request_defaults_are_safe():
    from api.server import CompressRequest, PlaygroundChatRequest
    c = CompressRequest(messages=["hello"])
    assert c.lossy is False and c.retrieval is False
    p = PlaygroundChatRequest(messages=["hello"])
    assert p.lossy is False and p.retrieval is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
