"""Which credential headers reach an upstream, and which must not.

Two failures live here and they pull in opposite directions:

  * FORWARD TOO LITTLE and the customer's request arrives unauthenticated. Azure
    OpenAI's SDK sends `api-key`, not Authorization; without it the upstream 401s,
    and a failed upstream writes no receipt, so the traffic is both broken and
    invisible.

  * FORWARD TOO MUCH, or to the wrong place, and a credential goes to a company
    the caller never named. That is the worse direction, and it is not
    hypothetical: a rejected x-brevitas-upstream used to be silently ignored,
    falling through to api.openai.com with `authorization` attached.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from brevitas.proxy import (
    _ALLOWED_UPSTREAMS,
    _passthrough_headers,
    get_openai_compatible_upstream,
)


class _Request:
    """Minimal stand-in: _passthrough_headers only reads .headers."""

    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_azure_api_key_is_NOT_forwarded_on_the_openai_family_lanes():
    """The regression this file was originally written with, now inverted.

    `api-key` is Azure's native credential. This shared forward set serves the
    four OPENAI-FAMILY call sites, whose upstreams are OpenAI, DeepSeek, Groq,
    xAI and the resellers. Forwarding it here sends an Azure customer's key to
    whichever of those the model prefix routes to -- a credential arriving at a
    company the caller never named, which is strictly worse than the 401 it was
    meant to fix.

    An earlier version of this test asserted the opposite and was wrong. The
    Azure lane forwards `api-key` itself, to a host built from a validated
    resource label; that is the only place it belongs.
    """
    headers = _passthrough_headers(
        _Request({"api-key": "azure-secret", "content-type": "application/json"}),
        "openai",
    )
    assert "api-key" not in {k.lower() for k in headers}, (
        "an Azure credential is being forwarded on an OpenAI-family lane, so it "
        "will reach whichever company the model prefix routes to"
    )


def test_the_azure_lane_forwards_the_credential_itself():
    """...and the customer is still authenticated, via the dedicated lane."""
    from brevitas.proxy import _azure_credential

    assert _azure_credential(_Request({"api-key": "azure-secret"})) == "azure-secret"
    assert _azure_credential(
        _Request({"authorization": "Bearer entra-token"})) == "Bearer entra-token"


def test_entra_bearer_still_reaches_the_upstream():
    """Managed-identity/Entra callers send Authorization instead of api-key."""
    headers = _passthrough_headers(
        _Request({"authorization": "Bearer entra-token"}), "openai")
    assert headers.get("authorization") == "Bearer entra-token"


def test_anthropic_keeps_its_own_credential_shape():
    headers = _passthrough_headers(
        _Request({"x-api-key": "anthropic-secret"}), "anthropic")
    assert headers.get("x-api-key") == "anthropic-secret"
    # The version header is defaulted rather than required from the caller.
    assert headers.get("anthropic-version")


def test_unrelated_headers_are_not_forwarded():
    """The forward set is an allowlist; anything else stays with us."""
    headers = _passthrough_headers(
        _Request({
            "x-brevitas-key": "our-own-credential",
            "cookie": "session=abc",
            "x-forwarded-for": "203.0.113.1",
        }),
        "openai",
    )
    for leaked in ("x-brevitas-key", "cookie", "x-forwarded-for"):
        assert leaked not in {k.lower() for k in headers}, (
            f"{leaked} was forwarded upstream; the forward set must stay an "
            "allowlist, and our OWN credential must never leave the building"
        )


def test_an_allowlisted_upstream_override_is_honoured():
    allowed = sorted(_ALLOWED_UPSTREAMS)[0]
    assert get_openai_compatible_upstream("gpt-4o", allowed) == allowed


def test_no_override_routes_by_model_as_before():
    assert get_openai_compatible_upstream("gpt-4o", None).startswith("https://")


def test_a_rejected_upstream_override_is_refused_not_ignored():
    """The regression test for a credential-redirect.

    Silently falling back re-routes the request to a different company than the
    caller named, and `authorization` goes with it. A customer aiming at their
    own Azure resource with a mistyped host had their Entra bearer transmitted to
    OpenAI, with a 200 back and nothing to see.
    """
    with pytest.raises(HTTPException) as caught:
        get_openai_compatible_upstream("gpt-4o", "https://evil.example.com")
    assert caught.value.status_code == 400


@pytest.mark.parametrize("hostile", [
    "http://169.254.169.254",            # cloud metadata
    "https://api.openai.com.evil.test",  # suffix confusion
    "file:///etc/passwd",
])
def test_hostile_overrides_are_refused(hostile):
    with pytest.raises(HTTPException):
        get_openai_compatible_upstream("gpt-4o", hostile)
