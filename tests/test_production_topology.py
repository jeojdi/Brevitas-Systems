import asyncio
import json
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import requests
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def test_docker_context_excludes_secrets_databases_and_build_outputs():
    patterns = {
        line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for required in {
        ".git", ".env*", "api/.secret_key", "api/*.db", "node_modules",
        "**/node_modules", ".venv", "**/__pycache__", "*.py[cod]", ".next",
        "build", "dist", "coverage",
    }:
        assert required in patterns


def test_railway_templates_split_api_workers_and_private_compressor():
    api = _json("railway.json")
    api_toml = tomllib.loads((ROOT / "railway.toml").read_text())
    worker = _json("deploy/railway-worker.json")
    compressor = _json("deploy/railway.json")

    assert api["build"]["dockerfilePath"] == "Dockerfile"
    assert api["deploy"]["numReplicas"] >= 2
    assert api["deploy"]["healthcheckPath"] == "/v1/health/ready"
    assert api_toml["deploy"]["numReplicas"] >= 2
    assert api_toml["deploy"]["healthcheckPath"] == "/v1/health/ready"

    assert worker["build"]["dockerfilePath"] == "Dockerfile"
    assert worker["deploy"]["numReplicas"] >= 2
    assert "BREVITAS_WORKER_BILLING_ROLE=authoritative" in worker["deploy"]["startCommand"]
    assert worker["deploy"]["startCommand"].endswith("python -m api.worker")
    assert worker["deploy"]["healthcheckPath"] == "/ready"

    assert compressor["build"]["dockerfilePath"] == "services/compress/Dockerfile"
    assert compressor["deploy"]["healthcheckPath"] == "/ready"
    # Public domains cannot be created by config-as-code; the operator guide must explicitly
    # prohibit one and use Railway private DNS.
    deployment_guide = (ROOT / "DEPLOYMENT_GUIDE.md").read_text()
    compressor_guide = (ROOT / "docs/DEPLOY_COMPRESS.md").read_text()
    assert "Do not add a Railway public domain to the compressor" in deployment_guide
    assert ".railway.internal" in compressor_guide
    assert "no generated domain" in compressor_guide
    compressor_dockerfile = (ROOT / "services/compress/Dockerfile").read_text()
    assert "COPY brevitas/security/redaction.py brevitas/security/redaction.py" in compressor_dockerfile
    assert "COPY brevitas/security/ brevitas/security/" not in compressor_dockerfile


def test_cloud_run_staging_uses_keyless_service_identity_and_worker_pool():
    api = (ROOT / "deploy/cloud-run-api-staging.yaml").read_text()
    worker = (ROOT / "deploy/cloud-run-worker-staging.yaml").read_text()
    cloud_build = (ROOT / "deploy/cloudbuild-api.yaml").read_text()
    runtime_identity = (
        "brevitas-staging-runtime@divine-camera-465917-j7.iam.gserviceaccount.com"
    )
    kms_key = (
        "projects/divine-camera-465917-j7/locations/global/keyRings/"
        "brevitas-staging/cryptoKeys/credential-envelope"
    )

    assert "kind: Service" in api
    assert "kind: WorkerPool" in worker
    assert 'run.googleapis.com/manualInstanceCount: "1"' in worker
    for manifest in (api, worker):
        assert f"serviceAccountName: {runtime_identity}" in manifest
        assert "name: BREVITAS_KMS_REQUIRED\n              value: \"true\"" in manifest
        assert f"value: {kms_key}" in manifest
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in manifest
        assert "serviceAccountKey" not in manifest
        assert "brevitas-staging-redis-url" in manifest
        assert 'run.googleapis.com/vpc-access-egress: all-traffic' in manifest
        assert '"network":"brevitas-staging-vpc"' in manifest
        assert '"subnetwork":"brevitas-staging-run-us-west1"' in manifest

    assert "name: gcr.io/cloud-builders/docker" in cloud_build
    assert "BREVITAS_BUILD_SHA=${_BREVITAS_BUILD_SHA}" in cloud_build
    assert "api:${_BREVITAS_BUILD_SHA}" in cloud_build


def test_shared_dependencies_are_same_region_tls_and_non_authoritative():
    guide = (ROOT / "DEPLOYMENT_GUIDE.md").read_text()
    assert "one primary US region" in guide
    assert "Supavisor transaction pooler" in guide
    assert "REDIS_URL=rediss://" in guide
    assert "multi-zone, TLS-only" in guide
    assert "AOF every second" in guide
    assert "Postgres remains authoritative after every Redis loss" in guide


def test_vercel_has_no_authoritative_scheduler():
    vercel = _json("vercel.json")
    assert "crons" not in vercel
    guide = (ROOT / "DEPLOYMENT_GUIDE.md").read_text()
    assert "Stripe checkout and webhook routes remain on Vercel" in guide
    assert "manual recovery control only" in guide


def test_production_compressor_address_must_be_private(monkeypatch):
    import api.server as server

    monkeypatch.setenv("BREVITAS_ENV", "production")
    assert server._private_compressor_url("http://compressor.railway.internal:8080") is True
    assert server._private_compressor_url("https://compressor.example.com") is False
    assert server._private_compressor_url("http://127.0.0.1:8080") is False


@pytest.mark.parametrize("marker", ["K_SERVICE", "K_REVISION", "CLOUD_RUN_WORKER_POOL"])
def test_cloud_run_platform_markers_enable_hosted_fail_closed_runtime(monkeypatch, marker):
    import api.server as server

    monkeypatch.delenv("BREVITAS_ENV", raising=False)
    monkeypatch.setenv(marker, "brevitas-staging")
    assert server._production_runtime() is True


@pytest.mark.parametrize(("url", "token", "message"), [
    ("https://compressor.example.com", "internal-token", "private networking"),
    ("http://compressor.railway.internal:8080", "", "BREVITAS_COMPRESS_TOKEN"),
])
def test_unsafe_configured_compressor_fails_production_startup_before_probe(
        monkeypatch, url, token, message):
    import api.server as server

    probe_called = False

    async def forbidden_probe():
        nonlocal probe_called
        probe_called = True
        raise AssertionError("unsafe compressor must not be probed")

    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("BREVITAS_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    monkeypatch.setenv("COMPANY_ADMIN_CURSOR_SECRET", "c" * 40)
    monkeypatch.setenv("BREVITAS_COMPRESS_URL", url)
    if token:
        monkeypatch.setenv("BREVITAS_COMPRESS_TOKEN", token)
    else:
        monkeypatch.delenv("BREVITAS_COMPRESS_TOKEN", raising=False)
    monkeypatch.setattr(server, "_compressor_status", forbidden_probe)

    async def start_application():
        async with server._lifespan(server.app):
            raise AssertionError("unsafe production config must not finish startup")

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(start_application())
    assert probe_called is False


def test_absent_optional_compressor_is_valid_in_production(monkeypatch):
    import api.server as server

    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    monkeypatch.setenv("COMPANY_ADMIN_CURSOR_SECRET", "c" * 40)
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://brevitassystems.com")
    monkeypatch.delenv("BREVITAS_COMPRESS_URL", raising=False)
    monkeypatch.delenv("BREVITAS_COMPRESS_TOKEN", raising=False)
    server._validate_runtime_config()


def test_production_requires_shared_cursor_secret(monkeypatch):
    import api.server as server

    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://brevitassystems.com")
    monkeypatch.delenv("BREVITAS_COMPRESS_URL", raising=False)
    monkeypatch.delenv("BREVITAS_COMPRESS_TOKEN", raising=False)
    monkeypatch.setenv("COMPANY_ADMIN_CURSOR_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="COMPANY_ADMIN_CURSOR_SECRET"):
        server._validate_runtime_config()

    monkeypatch.setenv("COMPANY_ADMIN_CURSOR_SECRET", "c" * 40)
    server._validate_runtime_config()


def test_production_requires_explicit_cors_allowlist_and_tls_redis(monkeypatch):
    import api.server as server

    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    monkeypatch.setenv("COMPANY_ADMIN_CURSOR_SECRET", "c" * 40)
    monkeypatch.delenv("BREVITAS_COMPRESS_URL", raising=False)
    monkeypatch.delenv("BREVITAS_COMPRESS_TOKEN", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        server._validate_runtime_config()
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://brevitassystems.com,*")
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        server._validate_runtime_config()
    monkeypatch.setenv("ALLOWED_ORIGINS", " , ")
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        server._validate_runtime_config()

    monkeypatch.setenv(
        "ALLOWED_ORIGINS",
        " , https://brevitassystems.com, https://www.brevitassystems.com, ",
    )
    assert server._configured_allowed_origins() == [
        "https://brevitassystems.com",
        "https://www.brevitassystems.com",
    ]
    monkeypatch.setenv("REDIS_URL", "redis://limits.internal:6379/0")
    with pytest.raises(RuntimeError, match="rediss://"):
        server._validate_runtime_config()

    monkeypatch.setenv("REDIS_URL", "rediss://limits.internal:6380/0")
    server._validate_runtime_config()


def test_hosted_job_dispatcher_requires_tls_redis(monkeypatch):
    from api.jobs import RedisJobDispatcher

    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("REDIS_URL", "redis://jobs.internal:6379/0")
    with pytest.raises(RuntimeError, match="rediss://"):
        RedisJobDispatcher()

    monkeypatch.setenv("BREVITAS_ENV", "development")
    dispatcher = RedisJobDispatcher()
    assert dispatcher.redis is not None


def test_job_dispatcher_configures_bounded_redis_timeouts(monkeypatch):
    from redis.asyncio import Redis

    from api.jobs import RedisJobDispatcher

    configured = {}
    redis_client = object()

    def from_url(url, **kwargs):
        configured["url"] = url
        configured.update(kwargs)
        return redis_client

    monkeypatch.setattr(Redis, "from_url", staticmethod(from_url))
    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("REDIS_URL", "rediss://jobs.internal:6380/0")

    dispatcher = RedisJobDispatcher()

    assert dispatcher.redis is redis_client
    assert configured == {
        "url": "rediss://jobs.internal:6380/0",
        "decode_responses": True,
        "socket_connect_timeout": 2,
        "socket_timeout": 10,
        "health_check_interval": 30,
    }


def test_production_fails_closed_without_postgres_or_job_crypto(monkeypatch):
    from api.security import credential_cipher_from_environment
    from api.store import make_store

    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("BREVITAS_STORE", "sqlite")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("BREVITAS_KMS_PROVIDER", "managed-test")
    monkeypatch.setenv("BREVITAS_KMS_KEY_ID", "production-key")
    monkeypatch.setenv("BREVITAS_KMS_KEY_VERSION", "1")
    monkeypatch.delenv("BREVITAS_LOCAL_KMS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Production requires"):
        make_store()
    with pytest.raises(RuntimeError, match="adapter is unavailable"):
        credential_cipher_from_environment()


def test_production_rejects_disabled_proxy_auth_and_never_uses_local_admission(monkeypatch):
    import api.server as server

    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("BREVITAS_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "false")
    with pytest.raises(RuntimeError, match="BREVITAS_PROXY_AUTH=true"):
        server._validate_runtime_config()

    async def start_application():
        async with server._lifespan(server.app):
            raise AssertionError("invalid production config must not finish startup")

    with pytest.raises(RuntimeError, match="BREVITAS_PROXY_AUTH=true"):
        asyncio.run(start_application())

    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")

    class Store:
        def cache_enabled(self, *_args):
            return False

    class Limiter:
        async def acquire(self, *_args, **_kwargs):
            return SimpleNamespace(
                allowed=True, _limiter=None, retry_after=0, reason="",
                remaining_requests=1, reset_seconds=1,
            )

    monkeypatch.setattr(server, "_store", Store())
    monkeypatch.setattr(server, "_distributed_limiter", Limiter())
    monkeypatch.setattr(server, "_auth_context_for_key", lambda *_args: server.AuthContext(
        key_hash="opaque_key", organization_id="org_1", customer_id="customer_1",
        scopes=frozenset({"proxy:invoke"}),
    ))
    server._proxy_windows.clear()

    async def receive():
        return {"type": "http.request", "body": b'{"model":"gpt-4o"}',
                "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/v1/chat/completions",
        "headers": [(b"x-brevitas-key", b"bvt_test")],
        "client": ("127.0.0.1", 1), "query_string": b"",
        "server": ("test", 80), "scheme": "http",
    }, receive)

    async def call_next(_request):
        raise AssertionError("production request must fail before proxying")

    response = asyncio.run(server._protect_model_proxy(request, call_next))
    assert response.status_code == 503
    assert len(server._proxy_windows) == 0


def test_api_live_probe_ignores_dependencies_but_ready_probe_does_not(monkeypatch):
    import api.server as server

    class Store:
        def healthy(self):
            return False

    class Limiter:
        async def healthy(self):
            return False

    monkeypatch.setattr(server, "_store", Store())
    monkeypatch.setattr(server, "_distributed_limiter", Limiter())
    async def compressor_ready():
        return {
            "configured": True,
            "internal_auth_configured": True,
            "private_endpoint": True,
            "reachable": True,
            "model_loaded": True,
        }

    monkeypatch.setattr(server, "_compressor_status", compressor_ready)
    server.app.state.accepting_traffic = True

    assert asyncio.run(server.liveness()) == {"status": "ok"}
    ready = asyncio.run(server.health())
    assert isinstance(ready, JSONResponse)
    assert ready.status_code == 503


def test_api_readiness_fails_closed_on_kms_without_changing_liveness(monkeypatch):
    import api.server as server

    class Store:
        def healthy(self):
            return True

    class Limiter:
        async def healthy(self):
            return True

    async def compressor_ready():
        return {
            "configured": True,
            "internal_auth_configured": True,
            "private_endpoint": True,
            "reachable": True,
            "model_loaded": True,
        }

    async def kms_unavailable():
        return {"configured": True, "active_probe": False, "fresh": False}

    monkeypatch.setattr(server, "_store", Store())
    monkeypatch.setattr(server, "_distributed_limiter", Limiter())
    monkeypatch.setattr(server, "_compressor_status", compressor_ready)
    monkeypatch.setattr(server, "_kms_readiness_status", kms_unavailable)
    monkeypatch.setattr(server.app.state, "accepting_traffic", True)
    monkeypatch.setenv("BREVITAS_COMPRESS_REQUIRED", "true")

    assert asyncio.run(server.liveness()) == {"status": "ok"}
    response = asyncio.run(server.health())
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["kms_ready"] is False
    assert payload["dependencies"]["kms"] == {
        "status": "unavailable",
        "configured": True,
        "active_probe": False,
        "fresh": False,
    }


def test_compressor_probe_is_nonblocking_single_flight(monkeypatch):
    import api.server as server

    calls = 0

    def slow_probe(_url, _timeout, base):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return {**base, "reachable": True, "model_loaded": True}

    monkeypatch.setenv("BREVITAS_COMPRESS_URL", "http://compressor.railway.internal:8080")
    monkeypatch.setenv("BREVITAS_COMPRESS_TOKEN", "internal-test-token")
    monkeypatch.setattr(server, "_compressor_probe", slow_probe)
    server._COMPRESSOR_STATUS.update(ts=0.0, data=None)
    monkeypatch.setattr(server, "_COMPRESSOR_INFLIGHT", None)

    async def exercise():
        ticked = asyncio.Event()

        async def ticker():
            await asyncio.sleep(0.005)
            ticked.set()

        first, second, _ = await asyncio.gather(
            server._compressor_status(), server._compressor_status(), ticker())
        return first, second, ticked.is_set()

    first, second, ticked = asyncio.run(exercise())
    assert ticked is True
    assert calls == 1
    assert first == second
    assert first["model_loaded"] is True


def test_compressor_probe_timeout_waves_keep_one_underlying_probe(monkeypatch):
    import api.server as server

    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def blocked_probe(_url, _timeout, base):
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(1)
        return {**base, "reachable": True, "model_loaded": True}

    monkeypatch.setenv("BREVITAS_COMPRESS_URL", "http://compressor.railway.internal:8080")
    monkeypatch.setenv("BREVITAS_COMPRESS_TOKEN", "internal-test-token")
    monkeypatch.setenv("BREVITAS_COMPRESS_PROBE_WAIT_SECONDS", "0.01")
    monkeypatch.setattr(server, "_compressor_probe", blocked_probe)
    server._COMPRESSOR_STATUS.update(ts=0.0, data=None)
    monkeypatch.setattr(server, "_COMPRESSOR_INFLIGHT", None)

    async def wave():
        return await asyncio.gather(*(server._compressor_status() for _ in range(5)))

    first_wave = asyncio.run(wave())
    assert entered.wait(1)
    second_wave = asyncio.run(wave())
    assert calls == 1
    assert all(result["reachable"] is False for result in first_wave + second_wave)
    with server._COMPRESSOR_STATUS_LOCK:
        future = server._COMPRESSOR_INFLIGHT
    assert future is not None and not future.done()

    release.set()
    future.result(timeout=1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with server._COMPRESSOR_STATUS_LOCK:
            if server._COMPRESSOR_INFLIGHT is None:
                break
        time.sleep(0.005)
    with server._COMPRESSOR_STATUS_LOCK:
        assert server._COMPRESSOR_INFLIGHT is None
    final = asyncio.run(server._compressor_status())
    assert final["reachable"] is True
    assert calls == 1


def test_optional_compressor_degrades_without_failing_global_readiness(monkeypatch):
    import api.server as server

    async def unavailable():
        return {
            "configured": True,
            "internal_auth_configured": True,
            "private_endpoint": True,
            "reachable": False,
            "model_loaded": False,
        }

    class Store:
        def healthy(self):
            return True

    class Limiter:
        async def healthy(self):
            return True

    monkeypatch.setattr(server, "_compressor_status", unavailable)
    monkeypatch.setattr(server, "_store", Store())
    monkeypatch.setattr(server, "_distributed_limiter", Limiter())
    monkeypatch.setattr(server.app.state, "accepting_traffic", True)
    monkeypatch.setenv("BREVITAS_COMPRESS_REQUIRED", "false")
    optional = asyncio.run(server.health())
    assert optional["status"] == "degraded"
    assert optional["dependencies"]["compressor"]["required"] is False

    monkeypatch.setenv("BREVITAS_COMPRESS_REQUIRED", "true")
    required = asyncio.run(server.health())
    assert isinstance(required, JSONResponse)
    assert required.status_code == 503


def test_provider_backends_use_accepted_sync_pool_contract(monkeypatch):
    import api.server as server

    calls = []
    responses = []
    payloads = iter([
        {"response": "ollama"},
        {"content": [{"text": "anthropic"}]},
        {"choices": [{"message": {"content": "openai-compatible"}}]},
    ])

    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.closed = False

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

        def close(self):
            self.closed = True

    class Pool:
        def request(self, provider, operation, method, url, **kwargs):
            calls.append((provider, operation, method, url, kwargs))
            response = Response(next(payloads))
            responses.append(response)
            return response

    monkeypatch.setattr(server, "provider_sync_http", Pool())
    assert server._make_ollama_backend("model")("prompt", "") == "ollama"
    assert server._make_anthropic_backend("secret", "model")("prompt", "") == "anthropic"
    assert server._make_openai_compat_backend(
        "openai", "secret", "model", "https://provider.invalid/v1",
    )("prompt", "") == "openai-compatible"
    assert [(call[0], call[1]) for call in calls] == [
        ("ollama", "generate"),
        ("anthropic", "messages"),
        ("openai", "chat.completions"),
    ]
    assert all(response.closed for response in responses)


def test_provider_errors_are_generic_and_pool_waits_for_threads(monkeypatch, caplog):
    import api.server as server
    from brevitas.provider_reliability import ProviderCircuitOpen

    class Pool:
        def request(self, *_args, **_kwargs):
            raise ProviderCircuitOpen(3.2)

    monkeypatch.setattr(server, "provider_sync_http", Pool())
    with pytest.raises(HTTPException) as unavailable:
        server._make_anthropic_backend("SENTINEL-SECRET", "model")("prompt", "")
    assert unavailable.value.status_code == 503
    assert unavailable.value.headers == {"Retry-After": "4"}
    assert "SENTINEL" not in unavailable.value.detail

    class TransportFailurePool:
        def request(self, *_args, **_kwargs):
            raise httpx.ConnectError("SENTINEL-TRANSPORT-DETAIL")

    monkeypatch.setattr(server, "provider_sync_http", TransportFailurePool())
    with pytest.raises(HTTPException) as transport:
        server._make_ollama_backend("model")("private prompt", "")
    assert transport.value.status_code == 502
    assert "SENTINEL" not in transport.value.detail
    assert "SENTINEL-TRANSPORT-DETAIL" not in caplog.text

    entered = threading.Event()
    release = threading.Event()

    def call():
        with server._provider_call():
            entered.set()
            release.wait(1)

    thread = threading.Thread(target=call)
    thread.start()
    assert entered.wait(1)
    assert server._wait_for_provider_calls(0.01) is False
    release.set()
    thread.join(1)
    assert server._wait_for_provider_calls(0.1) is True


def test_admin_breakdown_delegates_cursor_pagination_to_store(monkeypatch):
    import api.server as server

    captured = {}

    class Store:
        def get_admin_report_page(self, filters, **kwargs):
            captured.update(filters=filters, **kwargs)
            return {
                "rows": [{"account_id": "a"}], "totals": {"total_calls": 1},
                "pagination": {"total": 1, "limit": 25, "next_cursor": "", "has_more": False},
                "sort": kwargs["sort"], "direction": kwargs["direction"],
            }

    monkeypatch.setattr(server, "_store", Store())
    result = server.admin_stats_breakdown.__wrapped__(
        request=None, range="30d", account="account", project="project", client="client",
        provider="openai", model="model", sort="calls", direction="asc", limit=25,
        cursor="opaque-cursor", _="admin",
    )
    assert captured["cursor"] == "opaque-cursor"
    assert captured["limit"] == 25
    assert captured["sort"] == "calls"
    assert captured["direction"] == "asc"
    assert result["range"] == "30d"
    assert result["pagination"]["next_cursor"] == ""

    server.app.dependency_overrides[server._admin_authenticated] = lambda: "admin"
    monkeypatch.delenv("BREVITAS_COMPRESS_URL", raising=False)
    server._COMPRESSOR_STATUS.update(ts=0.0, data=None)
    try:
        with TestClient(server.app) as client:
            too_long = client.get(
                "/v1/admin/stats/breakdown",
                params={"cursor": "x" * 513},
            )
        assert too_long.status_code == 422
    finally:
        server.app.dependency_overrides.clear()


def test_worker_live_and_ready_probes_are_distinct(monkeypatch):
    import api.worker as worker

    async def unavailable():
        return False, False

    monkeypatch.setattr(worker, "_dependencies_ready", unavailable)
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    assert asyncio.run(worker.liveness()) == {"status": "ok"}
    ready = asyncio.run(worker.readiness())
    assert isinstance(ready, JSONResponse)
    assert ready.status_code == 503


def test_worker_readiness_fails_closed_on_kms_without_changing_liveness(monkeypatch):
    import api.worker as worker

    async def available():
        return True, True

    async def kms_unavailable():
        return {"configured": True, "active_probe": False, "fresh": False}

    monkeypatch.setattr(worker, "_dependencies_ready", available)
    monkeypatch.setattr(worker, "_kms_readiness_status", kms_unavailable)
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    monkeypatch.setattr(worker, "_BILLING_REQUIRED", False)
    assert asyncio.run(worker.liveness()) == {"status": "ok"}
    response = asyncio.run(worker.readiness())
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["dependencies"]["kms"]["status"] == "unavailable"


def test_worker_shutdown_and_lease_recovery_are_configured():
    source = (ROOT / "api/worker.py").read_text()
    durable_tests = (ROOT / "tests/test_durable_jobs.py").read_text()
    assert "BREVITAS_WORKER_DRAIN_SECONDS" in source
    assert "active jobs will recover by lease expiry" in source
    assert "test_expired_worker_lease_is_reclaimed" in durable_tests
    assert "test_redis_notification_failure_does_not_lose_durable_job" in durable_tests


def test_production_worker_requires_authoritative_billing_configuration(monkeypatch):
    import api.worker as worker

    monkeypatch.setattr(worker, "_production_runtime", lambda: True)
    monkeypatch.setenv("BREVITAS_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.delenv("BREVITAS_WORKER_BILLING_ROLE", raising=False)
    assert worker._billing_worker_role() == "authoritative"
    monkeypatch.setenv("BREVITAS_WORKER_BILLING_ROLE", "nonbilling")
    assert worker._billing_worker_role() == "nonbilling"

    monkeypatch.setenv("BREVITAS_WORKER_BILLING_ROLE", "authoritative")
    monkeypatch.setattr(worker, "_configure_managed_kms_from_deployment", lambda: None)
    monkeypatch.setattr(worker, "_initialize_credential_cipher", lambda required: object())
    monkeypatch.setattr(worker, "billing_recovery_is_configured", lambda: False)
    with pytest.raises(RuntimeError, match="billing recovery configuration is incomplete"):
        asyncio.run(worker.run())


def test_worker_readiness_exposes_required_billing_loop_state(monkeypatch):
    import api.worker as worker

    async def available():
        return True, True

    monkeypatch.setattr(worker, "_dependencies_ready", available)
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    monkeypatch.setattr(worker, "_BILLING_ROLE", "authoritative")
    monkeypatch.setattr(worker, "_BILLING_REQUIRED", True)
    monkeypatch.setattr(worker, "_BILLING_CONFIGURED", True)
    monkeypatch.setattr(worker, "_BILLING_LOOP_RUNNING", False)
    monkeypatch.setattr(worker, "_BILLING_HEALTH", {
        "running": False,
        "initial_validation_succeeded": False,
        "catalog_valid": False,
        "last_success_monotonic": 0.0,
        "consecutive_errors": 0,
        "last_error_monotonic": 0.0,
    })
    response = asyncio.run(worker.readiness())
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["dependencies"]["billing_recovery"] == {
        "status": "unavailable", "authoritative": True,
        "configured": True, "running": False, "role": "authoritative",
        "health": {
            "running": False,
            "initial_validation_succeeded": False,
            "catalog_valid": False,
            "last_success_fresh": False,
            "last_success_age_seconds": None,
            "consecutive_errors": 0,
            "error_threshold_exceeded": False,
        },
    }


def test_authoritative_billing_readiness_requires_fresh_valid_loop_health(monkeypatch):
    import api.worker as worker

    async def available():
        return True, True

    monkeypatch.setattr(worker, "_dependencies_ready", available)
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    monkeypatch.setattr(worker, "_BILLING_ROLE", "authoritative")
    monkeypatch.setattr(worker, "_BILLING_REQUIRED", True)
    monkeypatch.setattr(worker, "_BILLING_CONFIGURED", True)
    monkeypatch.setattr(worker, "_BILLING_LOOP_RUNNING", True)
    now = [100.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setenv("BREVITAS_BILLING_READINESS_STALE_SECONDS", "30")
    monkeypatch.setenv("BREVITAS_BILLING_READINESS_ERROR_THRESHOLD", "3")
    monkeypatch.setattr(worker, "_BILLING_HEALTH", {
        "running": False,
        "initial_validation_succeeded": False,
        "catalog_valid": False,
        "last_success_monotonic": 0.0,
        "consecutive_errors": 0,
        "last_error_monotonic": 0.0,
    })

    worker._report_billing_health({
        "running": True,
        "initial_validation_succeeded": False,
        "catalog_valid": True,
        "last_success_monotonic": 95.0,
        "consecutive_errors": 0,
        "last_error_monotonic": 0.0,
    })
    not_validated = asyncio.run(worker.readiness())
    assert isinstance(not_validated, JSONResponse)
    assert not_validated.status_code == 503

    worker._report_billing_health({
        "running": True,
        "initial_validation_succeeded": True,
        "catalog_valid": False,
        "last_success_monotonic": 95.0,
        "consecutive_errors": 0,
        "last_error_monotonic": 0.0,
    })
    invalid_catalog = asyncio.run(worker.readiness())
    assert isinstance(invalid_catalog, JSONResponse)
    assert invalid_catalog.status_code == 503

    worker._report_billing_health({
        "running": True,
        "initial_validation_succeeded": True,
        "catalog_valid": True,
        "last_success_monotonic": 95.0,
        "consecutive_errors": 0,
        "last_error_monotonic": 0.0,
    })
    healthy = asyncio.run(worker.readiness())
    assert healthy["status"] == "ok"
    assert healthy["dependencies"]["billing_recovery"]["health"][
        "last_success_fresh"] is True

    now[0] = 125.0
    still_fresh = asyncio.run(worker.readiness())
    assert still_fresh["status"] == "ok"
    now[0] = 125.001
    stale = asyncio.run(worker.readiness())
    assert isinstance(stale, JSONResponse)
    assert stale.status_code == 503
    stale_payload = json.loads(stale.body)
    assert stale_payload["dependencies"]["billing_recovery"]["health"][
        "last_success_fresh"] is False

    worker._report_billing_health({
        "running": True,
        "initial_validation_succeeded": True,
        "catalog_valid": True,
        "last_success_monotonic": 125.001,
        "consecutive_errors": 0,
        "last_error_monotonic": 0.0,
    })
    recovered_from_staleness = asyncio.run(worker.readiness())
    assert recovered_from_staleness["status"] == "ok"

    worker._report_billing_health({
        "running": True,
        "initial_validation_succeeded": True,
        "catalog_valid": True,
        "last_success_monotonic": 125.001,
        "consecutive_errors": 3,
        "last_error_monotonic": 99.0,
    })
    failed = asyncio.run(worker.readiness())
    assert isinstance(failed, JSONResponse)
    assert failed.status_code == 503
    payload = json.loads(failed.body)
    assert payload["dependencies"]["billing_recovery"]["health"][
        "error_threshold_exceeded"] is True

    worker._report_billing_health({
        "running": True,
        "initial_validation_succeeded": True,
        "catalog_valid": True,
        "last_success_monotonic": 125.001,
        "consecutive_errors": 2,
        "last_error_monotonic": 125.001,
    })
    recovered_from_errors = asyncio.run(worker.readiness())
    assert recovered_from_errors["status"] == "ok"


def test_kms_deployment_factory_registry_is_composed_before_readiness(monkeypatch):
    import api.server as server
    from brevitas.security import LocalTestKMS

    class ManagedTestKMS(LocalTestKMS):
        provider = "managed-test"
        is_managed = True

    adapter = ManagedTestKMS(b"m" * 32, environ={"BREVITAS_ENV": "test"})
    monkeypatch.setitem(server._managed_kms_factories, "railway", lambda: adapter)
    monkeypatch.setattr(server, "_managed_kms_adapter", None)
    monkeypatch.setattr(server, "_credential_cipher", None)
    monkeypatch.setattr(server, "_job_service", SimpleNamespace(crypto=None))
    monkeypatch.setenv("BREVITAS_KMS_ADAPTER_FACTORY", "registry:railway")
    server._configure_managed_kms_from_deployment()
    assert server._managed_kms_adapter is adapter


def test_kms_deployment_module_factory_requires_exact_allowlist(monkeypatch):
    import api.server as server
    from brevitas.security import KMSConfigurationError

    monkeypatch.setattr(server, "_managed_kms_adapter", None)
    monkeypatch.setenv("BREVITAS_KMS_ADAPTER_FACTORY", "untrusted.module:create")
    monkeypatch.setenv("BREVITAS_KMS_ADAPTER_TRUSTED_MODULES", "trusted.module")
    with pytest.raises(KMSConfigurationError, match="not trusted"):
        server._configure_managed_kms_from_deployment()


def test_production_compliance_router_uses_verified_db_derived_authority(monkeypatch):
    import api.server as server
    from api.compliance_admin import configure_compliance_admin

    organization_id = "00000000-0000-4000-8000-000000000101"
    other_organization = "00000000-0000-4000-8000-000000000102"
    actor_id = "00000000-0000-4000-8000-000000000103"
    request_id = "00000000-0000-4000-8000-000000000104"
    other_request = "00000000-0000-4000-8000-000000000105"

    class Store:
        def __init__(self):
            self.calls = []

        def member_organization(self, user_id):
            assert user_id == actor_id
            return {
                "id": organization_id, "name": "Authoritative company",
                "role": "company_owner", "billing_owner_id": actor_id,
            }

        def _request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            if path == "rpc/lock_company_actor_role":
                return "company_owner"
            if path == "rpc/compliance_submit_data_request":
                return {
                    "id": kwargs["data"]["p_request_id"], "status": "pending",
                }
            if method == "GET" and path == "data_subject_requests":
                params = kwargs["params"]
                if (params["organization_id"] == f"eq.{organization_id}"
                        and params["id"] == f"eq.{request_id}"):
                    return [{"id": request_id, "status": "pending"}]
                return []
            raise AssertionError((method, path))

    store = Store()

    def identity(request):
        auth = request.headers.get("authorization", "")
        if auth == "Bearer admin-session":
            return {
                "id": actor_id,
                "app_metadata": {
                    "role": "brevitas_admin",
                    "organization_id": other_organization,
                },
            }
        if auth == "Bearer nonadmin-session":
            return {
                "id": actor_id,
                "app_metadata": {
                    "role": "company_owner", "brevitas_admin": True,
                },
            }
        return {}

    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_identity", identity)
    server._configure_compliance_admin_runtime()
    client = TestClient(server.app)
    base = "/v1/admin/compliance"
    spoof_headers = {
        "X-Brevitas-Actor-ID": "attacker",
        "X-Brevitas-Organization-ID": other_organization,
    }
    try:
        assert client.get(f"{base}/requests/{request_id}").status_code == 403
        assert client.get(
            f"{base}/requests/{request_id}", headers=spoof_headers).status_code == 403
        assert client.get(
            f"{base}/requests/{request_id}",
            headers={"Authorization": "Bearer nonadmin-session"},
        ).status_code == 403

        admin_headers = {
            "Authorization": "Bearer admin-session", **spoof_headers,
        }
        submitted = client.post(f"{base}/requests", headers=admin_headers, json={
            "request_id": request_id, "request_type": "export", "scope": "tenant",
            "subject_id": None, "evidence_reference": "evidence:tenant:001",
        })
        assert submitted.status_code == 200
        submit_call = next(
            call for call in store.calls
            if call[1] == "rpc/compliance_submit_data_request")
        submit_data = submit_call[2]["data"]
        assert submit_data["p_organization_id"] == organization_id
        assert submit_data["p_actor_id"] == f"brevitas_admin:{actor_id}"
        assert other_organization not in str(submit_data)

        injected = client.post(f"{base}/requests", headers=admin_headers, json={
            "request_id": request_id, "request_type": "delete", "scope": "tenant",
            "subject_id": None, "evidence_reference": "evidence:tenant:002",
            "organization_id": other_organization, "actor_id": "attacker",
        })
        assert injected.status_code == 422
        assert client.get(
            f"{base}/requests/{other_request}", headers=admin_headers,
        ).status_code == 404

        mounted = set(server.app.openapi()["paths"])
        assert {
            f"{base}/requests", f"{base}/requests/{{request_id}}",
            f"{base}/requests/{{request_id}}/approve", f"{base}/hold-actions",
            f"{base}/hold-actions/{{action_id}}",
            f"{base}/hold-actions/{{action_id}}/approve",
        } <= mounted
        assert client.post(f"{base}/holds", headers=admin_headers, json={}).status_code == 404
    finally:
        configure_compliance_admin(None, None)
        server._compliance_admin_service = None


def test_production_compliance_dependency_and_config_fail_closed(monkeypatch):
    import api.server as server
    from api.compliance_admin import configure_compliance_admin

    organization_id = "00000000-0000-4000-8000-000000000101"
    actor_id = "00000000-0000-4000-8000-000000000103"
    request_id = "00000000-0000-4000-8000-000000000104"

    class Store:
        def member_organization(self, _user_id):
            raise requests.ConnectionError("SENTINEL-COMPLIANCE-DEPENDENCY")

        def _request(self, *_args, **_kwargs):
            raise AssertionError("membership failure must stop before workflow RPC")

    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setattr(server, "_store", Store())
    monkeypatch.setattr(server, "_dashboard_identity", lambda _request: {
        "id": actor_id, "app_metadata": {"role": "brevitas_admin"},
    })
    server._configure_compliance_admin_runtime()
    try:
        response = TestClient(server.app).get(
            f"/v1/admin/compliance/requests/{request_id}",
            headers={"Authorization": "Bearer admin-session",
                     "X-Brevitas-Organization-ID": organization_id},
        )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.json() == {"detail": "Membership verification unavailable"}
        assert "SENTINEL" not in response.text
    finally:
        configure_compliance_admin(None, None)
        server._compliance_admin_service = None

    monkeypatch.setattr(server, "_store", object())
    with pytest.raises(RuntimeError, match="requires Supabase"):
        server._configure_compliance_admin_runtime()
    unavailable = TestClient(server.app).get(
        f"/v1/admin/compliance/requests/{request_id}",
        headers={"Authorization": "Bearer admin-session"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Compliance administration unavailable"}


def test_hosted_dashboard_keys_use_atomic_store_contract(monkeypatch):
    import api.server as server

    organization_id = "00000000-0000-4000-8000-000000000001"
    actor_id = "00000000-0000-4000-8000-000000000002"
    key_id = "00000000-0000-4000-8000-000000000003"
    calls = []

    class Store:
        def _request(self, *_args, **_kwargs):
            raise AssertionError("server must use the store's atomic public contract")

        def create_key(self, *_args, **kwargs):
            calls.append(("create", kwargs))
            return {
                "api_key": "bvt_hosted_once", "secret_available_once": True,
                "key_id": key_id, "organization_id": organization_id,
                "key_type": "dashboard_session", "scopes": ["proxy:invoke"],
                "environment": "dashboard", "prefix": "bvt_hosted_",
                "expires_at": kwargs["expires_at"],
            }

        def list_organization_keys(self, _organization_id):
            return [{"id": key_id, "key_type": "dashboard_session"}]

        def revoke_organization_key(self, *_args, **kwargs):
            calls.append(("revoke", kwargs))
            return True

    monkeypatch.setattr(server, "_store", Store())
    monkeypatch.setattr(
        server, "_member_organization",
        lambda *_args, **_kwargs: (actor_id, {
            "id": organization_id, "role": "company_owner",
            "billing_owner_id": actor_id,
        }),
    )
    monkeypatch.setattr(
        server, "_active_company_membership",
        lambda _actor: (organization_id, "company_owner"),
    )
    client = TestClient(server.app)
    service = client.post("/v1/keys", json={"purpose": "service"})
    assert service.status_code == 409
    created = client.post("/v1/keys", json={"purpose": "dashboard_session"})
    assert created.status_code == 200
    assert created.json()["api_key"] == "bvt_hosted_once"
    assert created.json()["secret_available_once"] is True
    revoked = client.delete(f"/v1/keys/{key_id}")
    assert revoked.status_code == 200
    assert [name for name, _ in calls] == ["create", "revoke"]
    assert calls[0][1]["actor_role"] == "company_owner"
    assert calls[0][1]["request_id"]
    assert calls[1][1]["actor_role"] == "company_owner"
    assert calls[1][1]["request_id"]
