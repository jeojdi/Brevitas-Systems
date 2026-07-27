"""HTTP-level tests for the compression service.

These exercise the real FastAPI/Starlette request path with a TestClient so that
the oversized-body middleware, the request-body replay, and the ``Depends``-based
auth wiring are all covered end to end. The unit tests in
``test_compress_service_hardening.py`` call ``verify_token``/``optimize_prompt``
as plain functions and therefore would NOT catch a regression such as a removed
``Depends(verify_token)`` or a middleware that swallows the request body.
"""

import pytest
from fastapi.testclient import TestClient

from services.compress import app as compress


@pytest.fixture
def client(monkeypatch):
    # Token is required for the app to boot (lifespan fails closed otherwise).
    monkeypatch.setenv("BREVITAS_COMPRESS_TOKEN", "service-secret")
    # Avoid downloading/loading the real model; None => lossless passthrough,
    # which still returns a non-empty compressed_prompt.
    monkeypatch.setattr(compress, "load_model", lambda: None)
    with TestClient(compress.app) as test_client:
        yield test_client


def test_valid_request_replays_body_and_returns_200(client):
    """A real POST body must reach the route (regression: middleware body replay).

    Without setting request._body in reject_oversized_requests, Starlette 1.3.1's
    wrapped_receive hands the route an empty body and this 422s.
    """
    response = client.post(
        "/v1/optimize",
        headers={"Authorization": "Bearer service-secret"},
        json={"prompt": "The quick brown fox jumps over the lazy dog.", "rate": 1.0},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["compressed_prompt"], "optimized body must be non-empty"
    assert payload["tokens_before"] > 0
    assert payload["tokens_after"] > 0


def test_missing_token_returns_401(client):
    """No Authorization header -> 401 (auth actually wired via Depends)."""
    response = client.post(
        "/v1/optimize",
        json={"prompt": "hello world", "rate": 1.0},
    )
    assert response.status_code == 401, response.text


def test_wrong_token_returns_403(client):
    """Well-formed but incorrect token -> 403."""
    response = client.post(
        "/v1/optimize",
        headers={"Authorization": "Bearer not-the-secret"},
        json={"prompt": "hello world", "rate": 1.0},
    )
    assert response.status_code == 403, response.text


def test_malformed_authorization_header_returns_401(client):
    """A header that is not a single ``Bearer <token>`` pair -> 401."""
    response = client.post(
        "/v1/optimize",
        headers={"Authorization": "service-secret"},
        json={"prompt": "hello world", "rate": 1.0},
    )
    assert response.status_code == 401, response.text


def test_validation_error_does_not_echo_input_or_ctx(client):
    """422 responses must not carry ``input``/``ctx`` (which can hold prompt text)."""
    sentinel = "SENTINEL-CUSTOMER-PROMPT-DO-NOT-ECHO"
    response = client.post(
        "/v1/optimize",
        headers={"Authorization": "Bearer service-secret"},
        # rate below the allowed floor triggers a validation error.
        json={"prompt": sentinel, "rate": 0.0},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    for error in body["detail"]:
        assert "input" not in error
        assert "ctx" not in error
    assert sentinel not in response.text
