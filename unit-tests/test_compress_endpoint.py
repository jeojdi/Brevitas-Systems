"""End-to-end /v1/compress behaviour with the fake remote compressor.

Asserts the whole point of the change: a single long prompt now saves real tokens (10-60%),
only the LAST message is rewritten, code fences survive, and the kill-switch restores a
byte-identical passthrough.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import LONG_PROMPT


@pytest.fixture
def client(monkeypatch, fake_remote):
    _ = fake_remote  # activates the patched remote compressor + disabled gate (side effects)
    import api.server as server
    server.app.dependency_overrides[server._authenticated] = lambda: "test-key-hash"
    monkeypatch.setattr(server._store, "record_usage", lambda **_kw: None)
    # ensure the env kill-switch is ON for these tests
    monkeypatch.setenv("BREVITAS_COMPRESS_LOSSY", "1")
    with TestClient(server.app) as c:
        yield c
    server.app.dependency_overrides.clear()


def test_long_single_prompt_saves_in_band(client):
    resp = client.post("/v1/compress", json={"messages": [LONG_PROMPT], "lossy": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["message_reason"] == "compressed"
    assert 10.0 <= data["savings_pct"] <= 60.0, data["savings_pct"]
    assert data["optimized_tokens"] < data["baseline_tokens"]


def test_only_last_message_is_rewritten(client):
    stable = "SYSTEM: never change me. " * 5
    resp = client.post("/v1/compress", json={"messages": [stable, LONG_PROMPT], "lossy": True})
    data = resp.json()
    out = data["compressed_messages"]
    assert out[0] == stable                      # earlier message byte-identical (cache-safe)
    assert out[-1] != LONG_PROMPT                # last message compressed
    assert len(out) == 2


def test_kill_switch_env_forces_passthrough(client, monkeypatch):
    monkeypatch.setenv("BREVITAS_COMPRESS_LOSSY", "0")
    resp = client.post("/v1/compress", json={"messages": [LONG_PROMPT], "lossy": True})
    data = resp.json()
    assert data["compressed_messages"] == [LONG_PROMPT]   # unchanged
    assert data["message_reason"] == "lossy_disabled"


def test_per_request_lossy_false_forces_passthrough(client):
    resp = client.post("/v1/compress", json={"messages": [LONG_PROMPT], "lossy": False})
    data = resp.json()
    assert data["compressed_messages"] == [LONG_PROMPT]
    assert data["message_reason"] == "lossy_disabled"


def test_code_fence_in_last_message_survives(client):
    prose = ("Here is a long enough explanation paragraph that will be compressed because it is "
             "comfortably longer than the minimum segment threshold used by the router today.")
    fence = "```python\ndef add(a, b):\n    return  a  +  b\n```"
    msg = f"{prose}\n{fence}\n{prose}"
    resp = client.post("/v1/compress", json={"messages": [msg], "lossy": True})
    data = resp.json()
    assert "def add(a, b):\n    return  a  +  b" in data["compressed_messages"][-1]


def test_response_carries_reason_and_method(client):
    resp = client.post("/v1/compress", json={"messages": [LONG_PROMPT], "lossy": True})
    data = resp.json()
    assert "message_reason" in data and "method" in data
    assert "reason" in data            # prior-context retrieval reason still present
    assert "quality_sim" in data       # gate similarity (None when gate disabled)
    assert "message_latency_ms" in data and data["message_latency_ms"] >= 0.0
