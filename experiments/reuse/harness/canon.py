"""Canonicalisation and content addressing.

Every reuse key in this experiment is a sha256 over a canonical encoding. Canonicalisation
is the single most common cause of a falsely negative reuse result (false-negative item 3):
unstable key ordering, embedded timestamps, absolute paths and re-serialised bodies all make
two logically identical inputs hash differently, which looks exactly like the idea failing.

Everything here is therefore deliberately boring, and `selftest.py` hashes the same logical
input twice through the full path and asserts a match.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

# Substrings that must never appear inside a canonical encoding. Any of them means some
# run-local nondeterminism leaked into a reuse key.
_FORBIDDEN = (
    re.compile(r"/Users/[^/\s\"]+"),  # absolute home paths
    re.compile(r"/private/tmp/"),
    re.compile(r"/var/folders/"),
    re.compile(r"\b20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"),  # iso timestamps
    re.compile(r"\brun_[0-9a-f]{8,}\b"),  # run ids
)


class CanonError(ValueError):
    pass


def _scrub(value: Any, run_root: str | None) -> Any:
    """Recursively relativise paths under `run_root` so per-run scratch dirs do not leak."""
    if isinstance(value, str):
        if run_root and value.startswith(run_root):
            rel = os.path.relpath(value, run_root)
            return f"<run>/{rel}"
        return value
    if isinstance(value, dict):
        return {k: _scrub(v, run_root) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, run_root) for v in value]
    return value


def canonical(obj: Any, run_root: str | None = None) -> str:
    """Deterministic encoding: sorted keys, no insignificant whitespace, UTF-8, no NaN.

    `run_root` is stripped from any string that starts with it, so an operation performed in
    /scratch/run_a and the same operation in /scratch/run_b canonicalise identically. Without
    this, isolation (requirement 8.6) would destroy all cross-run reuse by construction.
    """
    scrubbed = _scrub(obj, run_root)
    text = json.dumps(
        scrubbed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    for pattern in _FORBIDDEN:
        hit = pattern.search(text)
        if hit:
            raise CanonError(
                f"run-local value leaked into canonical encoding: {hit.group(0)!r}. "
                "This would silently destroy cross-run reuse."
            )
    return text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: str) -> str:
    with open(path, "rb") as handle:
        return sha256_bytes(handle.read())


def reuse_key(
    *,
    op_type: str,
    args: Any,
    model: dict[str, Any] | None,
    artifact_hashes: dict[str, str],
    upstream_output_hashes: list[str],
    run_root: str | None = None,
) -> str:
    """The reuse key of a single operation.

    The last argument is the whole idea. Including the *output hashes* of upstream operations
    (in consumption order, because context order changes a model's result) makes the key
    compose transitively: if any ancestor's output changes, every descendant's key changes.
    Drop it and this is a memo table, not a dependency tracker.

    `artifact_hashes` is keyed by artifact identity and sorted by `canonical`, so the order in
    which a run happened to touch files does not affect the key.
    """
    payload = {
        "v": 1,
        "op_type": op_type,
        "args": args,
        "model": model,
        "artifacts": artifact_hashes,
        "upstream": upstream_output_hashes,
    }
    return sha256_text(canonical(payload, run_root=run_root))
