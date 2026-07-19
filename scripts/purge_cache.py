#!/usr/bin/env python3
"""Purge the Brevitas response cache.

Run this once after the safety remediation: existing cached rows predate the fix that
stops caching answers produced from retrieval-pruned or lossily-compressed context, so
their origin is unknown and any of them could replay a degraded answer as a verified
cache hit. This deletes every row from every configured cache backend.

Usage:
    python scripts/purge_cache.py            # purge the configured + local caches
    BREVITAS_CACHE_BACKEND=supabase \
      NEXT_PUBLIC_SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
      python scripts/purge_cache.py          # also purge the hosted Supabase cache

Safe to re-run; purging an empty cache removes 0 rows.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from anywhere: put the repo root on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brevitas.semantic_cache import SemanticCache, make_semantic_cache  # noqa: E402


def main() -> int:
    total = 0
    seen: set[str] = set()

    # 1. The configured backend (Supabase when opted in, else local SQLite).
    try:
        configured = make_semantic_cache()
        removed = configured.purge()
        backend = type(configured).__name__
        print(f"purged {removed} row(s) from {backend}")
        total += removed
        seen.add(getattr(configured, "db_path", backend))
    except Exception as exc:  # never leave the operator unsure — report and continue
        print(f"WARNING: configured backend purge failed: {type(exc).__name__}: {exc}")

    # 2. The local SQLite backend (the Playground and proxy share this DB by default).
    #    Skip if the configured backend already covered this exact file.
    try:
        local = SemanticCache()
        if getattr(local, "db_path", None) not in seen:
            removed = local.purge()
            print(f"purged {removed} row(s) from local SQLite ({local.db_path})")
            total += removed
    except Exception as exc:
        print(f"WARNING: local SQLite purge failed: {type(exc).__name__}: {exc}")

    print(f"done: {total} row(s) removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
