import asyncio
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from services.compress import app as compress


class _FakeModel:
    def __init__(self, *, delay: float = 0, error: Exception | None = None):
        self.delay = delay
        self.error = error
        self.active = 0
        self.peak = 0

    def compress_prompt(self, prompt, **_kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error:
                raise self.error
            return {"compressed_prompt": prompt}
        finally:
            self.active -= 1


def test_service_token_is_required_and_compared(monkeypatch):
    monkeypatch.delenv("BREVITAS_COMPRESS_TOKEN", raising=False)
    with pytest.raises(HTTPException) as missing_config:
        compress.verify_token("Bearer anything")
    assert missing_config.value.status_code == 503

    monkeypatch.setenv("BREVITAS_COMPRESS_TOKEN", "service-secret")
    with pytest.raises(HTTPException) as missing:
        compress.verify_token(None)
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException) as wrong:
        compress.verify_token("Bearer wrong")
    assert wrong.value.status_code == 403
    assert compress.verify_token("Bearer service-secret") is True


def test_request_bounds_are_enforced():
    with pytest.raises(ValidationError):
        compress.OptimizeRequest(prompt="x", rate=0)
    with pytest.raises(ValidationError):
        compress.OptimizeRequest(prompt="x", rate=1.01)
    with pytest.raises(ValidationError):
        compress.OptimizeRequest(prompt="")
    with pytest.raises(ValidationError):
        compress.OptimizeRequest(prompt="x", force_tokens=["x"] * (compress._MAX_FORCE_TOKENS + 1))


def test_readiness_fails_when_model_is_unavailable(monkeypatch):
    monkeypatch.setattr(compress, "_MODEL_LOADED", False)
    response = asyncio.run(compress.health_check())
    assert response.status_code == 503


def test_startup_liveness_and_readiness_are_distinct(monkeypatch):
    monkeypatch.setattr(compress, "_MODEL_LOAD_COMPLETE", True)
    monkeypatch.setattr(compress, "_MODEL_LOADED", True)
    monkeypatch.setattr(compress, "_ACCEPTING_TRAFFIC", True)
    assert asyncio.run(compress.liveness()) == {"status": "ok"}
    assert asyncio.run(compress.startup_check()) == {"status": "ok"}
    ready = asyncio.run(compress.health_check())
    assert ready.status == "ok"

    monkeypatch.setattr(compress, "_ACCEPTING_TRAFFIC", False)
    draining = asyncio.run(compress.health_check())
    assert draining.status_code == 503


def test_capacity_rejects_without_unbounded_wait(monkeypatch):
    monkeypatch.setattr(compress, "_inference_slots", asyncio.Semaphore(0))
    monkeypatch.setattr(compress, "_ADMISSION_TIMEOUT_S", 0.01)
    request = compress.OptimizeRequest(prompt="hello", rate=1)
    with pytest.raises(HTTPException) as overloaded:
        asyncio.run(compress.optimize_prompt(request, True))
    assert overloaded.value.status_code == 429
    assert overloaded.value.headers["Retry-After"] == "1"


def test_blocking_inference_does_not_block_event_loop(monkeypatch):
    fake = _FakeModel(delay=0.05)
    monkeypatch.setattr(compress, "_LLMLINGUA", fake)
    monkeypatch.setattr(compress, "_inference_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(compress, "_ADMISSION_TIMEOUT_S", 0.1)
    monkeypatch.setattr(compress, "_INFERENCE_TIMEOUT_S", 1)

    async def exercise():
        ticked = asyncio.Event()

        async def ticker():
            await asyncio.sleep(0.005)
            ticked.set()

        request = compress.OptimizeRequest(prompt="private prompt", rate=0.5)
        result, _ = await asyncio.gather(compress.optimize_prompt(request, True), ticker())
        return result, ticked.is_set()

    result, ticked = asyncio.run(exercise())
    assert ticked
    assert result.lossy is True
    assert fake.peak == 1


def test_failures_do_not_log_or_return_prompt(monkeypatch, caplog):
    sentinel = "SENTINEL-CUSTOMER-PROMPT"
    monkeypatch.setattr(compress, "_LLMLINGUA", _FakeModel(error=RuntimeError(sentinel)))
    monkeypatch.setattr(compress, "_inference_slots", asyncio.Semaphore(1))
    request = compress.OptimizeRequest(prompt=sentinel, rate=0.5)
    with pytest.raises(HTTPException) as failed:
        asyncio.run(compress.optimize_prompt(request, True))
    assert failed.value.status_code == 503
    assert sentinel not in failed.value.detail
    assert sentinel not in caplog.text


def test_chunked_body_is_bounded_without_content_length(monkeypatch):
    monkeypatch.setenv("BREVITAS_COMPRESS_TOKEN", "service-secret")
    monkeypatch.setattr(compress, "_MAX_BODY_BYTES", 32)
    monkeypatch.setattr(compress, "load_model", lambda: None)
    with TestClient(compress.app) as client:
        response = client.post(
            "/v1/optimize",
            headers={"Authorization": "Bearer service-secret"},
            content=iter([b"{" + b"x" * 20, b"x" * 20 + b"}"]),
        )
    assert response.status_code == 413
