import asyncio

import pytest

from api.build_info import build_identity, validate_production_build_identity


SHA = "a" * 40


def test_build_identity_publishes_only_validated_immutable_fields():
    identity = build_identity(environ={
        "BREVITAS_BUILD_SHA": SHA.upper(),
        "RAILWAY_GIT_COMMIT_SHA": SHA,
        "BREVITAS_BUILD_TIMESTAMP": "2026-07-20T13:15:18-07:00",
        "BREVITAS_BUILD_VERSION": "1.2.3+release.4",
        "BREVITAS_IMAGE_DIGEST": f"sha256:{'b' * 64}",
        "GITHUB_REF_NAME": "must-not-be-exposed",
        "PWD": "/must/not/be/exposed",
    })
    assert identity == {
        "commit_sha": SHA,
        "built_at": "2026-07-20T20:15:18Z",
        "version": "1.2.3+release.4",
        "image_digest": f"sha256:{'b' * 64}",
    }


@pytest.mark.parametrize("environ,message", [
    ({}, "requires a full immutable"),
    ({"BREVITAS_BUILD_SHA": "main"}, "full immutable"),
    ({"BREVITAS_BUILD_SHA": "a" * 40, "RAILWAY_GIT_COMMIT_SHA": "b" * 40},
     "Conflicting"),
    ({"BREVITAS_BUILD_SHA": SHA, "BREVITAS_BUILD_TIMESTAMP": "2026-07-20"},
     "timestamp"),
    ({"BREVITAS_BUILD_SHA": SHA, "BREVITAS_BUILD_VERSION": "latest"},
     "version"),
    ({"BREVITAS_BUILD_SHA": SHA, "BREVITAS_IMAGE_DIGEST": "sha256:latest"},
     "sha256 digest"),
])
def test_required_build_identity_fails_closed(environ, message):
    with pytest.raises(RuntimeError, match=message):
        build_identity(environ=environ, required=True)


def test_production_validation_requires_identity_but_local_development_does_not(monkeypatch):
    for name in (
        "BREVITAS_BUILD_SHA", "RAILWAY_GIT_COMMIT_SHA",
        "VERCEL_GIT_COMMIT_SHA", "GITHUB_SHA",
    ):
        monkeypatch.delenv(name, raising=False)
    validate_production_build_identity(False)
    with pytest.raises(RuntimeError, match="Production requires"):
        validate_production_build_identity(True)


def test_api_and_worker_expose_safe_version_contract(monkeypatch):
    import api.server as server
    import api.worker as worker

    monkeypatch.setenv("BREVITAS_BUILD_SHA", SHA)
    for name in ("RAILWAY_GIT_COMMIT_SHA", "VERCEL_GIT_COMMIT_SHA", "GITHUB_SHA"):
        monkeypatch.delenv(name, raising=False)
    assert asyncio.run(server.version()) == {
        "service": "api", "build": {"commit_sha": SHA},
    }
    assert asyncio.run(worker.version()) == {
        "service": "worker", "build": {"commit_sha": SHA},
    }


def test_api_production_startup_rejects_missing_identity_before_dependency_probe(monkeypatch):
    import api.server as server

    for name in (
        "BREVITAS_BUILD_SHA", "RAILWAY_GIT_COMMIT_SHA",
        "VERCEL_GIT_COMMIT_SHA", "GITHUB_SHA",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server, "_production_runtime", lambda: True)
    probe_called = False

    async def forbidden_probe():
        nonlocal probe_called
        probe_called = True

    monkeypatch.setattr(server, "_compressor_status", forbidden_probe)

    async def start():
        async with server._lifespan(server.app):
            raise AssertionError("production must not accept traffic")

    with pytest.raises(RuntimeError, match="Production requires"):
        asyncio.run(start())
    assert probe_called is False
