"""/v1/health surfaces the compressor status so silent lossless-fallback is visible."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import api.server as server
    with TestClient(server.app) as c:
        yield c


def test_health_reports_optional_compressor_degradation(client, monkeypatch):
    import api.server as server
    server._COMPRESSOR_STATUS.update(ts=0.0, data=None)   # bust the cache
    monkeypatch.delenv("BREVITAS_COMPRESS_URL", raising=False)
    response = client.get("/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "degraded"
    comp = data["compressor"]
    assert set(comp) == {
        "configured", "internal_auth_configured", "private_endpoint", "reachable",
        "model_loaded",
    }
    assert comp["configured"] is False           # no URL set -> not configured
    assert comp["reachable"] is False


def test_health_stays_available_but_degraded_in_production(client, monkeypatch):
    import api.server as server
    server._COMPRESSOR_STATUS.update(ts=0.0, data=None)
    monkeypatch.delenv("BREVITAS_COMPRESS_URL", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_health_configured_but_unreachable(client, monkeypatch):
    import api.server as server
    server._COMPRESSOR_STATUS.update(ts=0.0, data=None)
    monkeypatch.setenv("BREVITAS_COMPRESS_URL", "http://127.0.0.1:59999")  # nothing listening
    data = client.get("/v1/health").json()
    comp = data["compressor"]
    assert comp["configured"] is True
    assert comp["reachable"] is False
    assert comp["model_loaded"] is False


def test_health_never_returns_compressor_url_or_token(client, monkeypatch):
    import api.server as server
    server._COMPRESSOR_STATUS.update(ts=0.0, data=None)
    monkeypatch.setenv("BREVITAS_COMPRESS_URL", "https://compressor.example.com")
    monkeypatch.setenv("BREVITAS_COMPRESS_TOKEN", "SENTINEL-INTERNAL-TOKEN")
    monkeypatch.setenv("BREVITAS_COMPRESS_REQUIRED", "true")
    response = client.get("/v1/health/ready")
    serialized = response.text
    assert response.status_code == 503
    assert "compressor.example.com" not in serialized
    assert "SENTINEL-INTERNAL-TOKEN" not in serialized
    assert response.json()["compressor"]["private_endpoint"] is False
