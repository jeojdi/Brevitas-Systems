import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _reset_inprocess_rate_limiter():
    """Isolate the in-process slowapi rate-limit bucket between tests.

    The API rate-limit key_func (api.server._rate_key) now buckets solely on the
    verified network peer — a security fix, since the unverified X-Brevitas-Key /
    X-API-Key headers must not let a caller mint a fresh empty bucket per request.
    Under Starlette's TestClient every request reports client.host == "testclient",
    so the single in-memory slowapi bucket would otherwise be shared across the
    whole session and bleed 429s into unrelated tests once 10 requests accumulate.
    Resetting before each test restores per-test isolation without changing the
    production keying behavior. Only resets modules already imported.
    """
    for name in ("api.server", "api.proxy"):
        module = sys.modules.get(name)
        limiter = getattr(module, "limiter", None) if module else None
        if limiter is None:
            continue
        try:
            limiter.reset()
        except Exception:
            storage = getattr(limiter, "_storage", None)
            if storage is not None and hasattr(storage, "reset"):
                storage.reset()
    yield
