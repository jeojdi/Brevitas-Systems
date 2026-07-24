"""Hosting-platform detection shared by API, workers, stores, and coordination.

Hosted staging must fail closed in the same places as production. Platform
markers are preferable to treating every local ``BREVITAS_ENV=staging`` process
as hosted, which keeps local integration tests and operator tooling usable.
"""
from __future__ import annotations

import os
from collections.abc import Mapping


_HOST_MARKERS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_PROJECT_ID",
    "K_SERVICE",
    "K_REVISION",
    "CLOUD_RUN_WORKER_POOL",
)


def hosted_runtime(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this process is running in a hosted strict environment."""

    env = os.environ if environ is None else environ
    name = str(env.get("BREVITAS_ENV", "")).strip().lower()
    return name in {"prod", "production"} or any(env.get(key) for key in _HOST_MARKERS)


__all__ = ["hosted_runtime"]
