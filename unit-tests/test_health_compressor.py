"""/v1/health surfaces the compressor status so silent lossless-fallback is visible."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import api.server as server
    with TestClient(server.app) as c:
        yield c


def test_health_reports_compressor_block(client, monkeypatch):
    import api.server as server
    server._COMPRESSOR_STATUS.update(ts=0.0, data=None)   # bust the cache
    monkeypatch.delenv("BREVITAS_COMPRESS_URL", raising=False)
    data = client.get("/v1/health").json()
    assert data["status"] == "ok"
    comp = data["compressor"]
    assert set(comp) == {"configured", "reachable", "model_loaded"}
    assert comp["configured"] is False           # no URL set -> not configured
    assert comp["reachable"] is False


def test_health_configured_but_unreachable(client, monkeypatch):
    import api.server as server
    server._COMPRESSOR_STATUS.update(ts=0.0, data=None)
    monkeypatch.setenv("BREVITAS_COMPRESS_URL", "http://127.0.0.1:59999")  # nothing listening
    data = client.get("/v1/health").json()
    comp = data["compressor"]
    assert comp["configured"] is True
    assert comp["reachable"] is False
    assert comp["model_loaded"] is False
