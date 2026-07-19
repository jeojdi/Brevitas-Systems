"""
Semantic response cache — the real "memory" lever.

When a request arrives, check whether we have already answered the same (or a
reworded-but-equivalent) request. On a hit we return the stored response and skip
the upstream model call entirely → 100% token savings on that call, and it's faster.

Two layers, cheapest first:

  Layer 1 — exact hash: SHA-256 over the whole request (model, system, params,
            every message). Sub-millisecond, stdlib only, no embedding. Catches
            byte-identical repeats: retries, agent loops, parallel agents sharing
            context. Works even without the optional embedding dependency.

  Layer 2 — semantic: only on a Layer-1 miss. Embed the LAST user message locally
            and find the nearest prior request whose everything-else (system, tools,
            prior turns, params, model) is byte-identical — i.e. only the final
            question differs. Return it if cosine similarity >= threshold.

Safety (conservative by design):
  * model_id is part of BOTH hashes → a response is NEVER served to a different
    model. Same-model-only, for free.
  * Only deterministic-ish calls are cached: no tools, not streaming, and effective
    temperature <= max_temperature. High-temperature (intentionally random) calls
    pass straight through.
  * TTL with jitter bounds staleness; expired rows are ignored and recomputed.

Backend: SQLite file (shared by every agent on this proxy, survives restart). The
nearest-neighbour scan is brute-force cosine over the context bucket — that bucket
only holds rows with an identical prefix, so it is naturally tiny.
# ponytail: O(n) scan per bucket, fine to ~10k rows; swap for pgvector (migration
# 002) when hosted/large.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _embed

try:
    import numpy as np
except Exception:  # numpy ships with the semanticcache extra; without it, Layer 1 only
    np = None


@dataclass
class CacheHit:
    kind: str                    # "exact" | "semantic"
    response: dict               # the provider's own response JSON, replayed verbatim
    prompt_tokens: int
    completion_tokens: int
    similarity: float = 1.0


class SemanticCache:
    def __init__(
        self,
        db_path: str | None = None,
        *,
        # 0.97 is measured, not arbitrary. With bge-small, look-alike-but-DIFFERENT
        # questions score up to ~0.94 ("2+2" vs "2+3" = 0.938; "order #123" vs "#999"
        # = 0.923), which OVERLAPS loose paraphrases. 0.97 sits above that whole band,
        # so a hit is never a different-answer look-alike — at the cost of missing looser
        # rewordings (e.g. 0.959). Do NOT lower this to raise hit-rate: below ~0.94 you
        # start serving wrong answers. To widen safely, add an LLM-judge to verify a
        # candidate hit (the "verified semantic cache" pattern), don't drop the floor.
        similarity_threshold: float = 0.97,
        max_temperature: float = 0.5,         # above this, don't cache (intentional randomness)
        default_ttl_s: int = 3600,
        # Tenant isolation: mixed into BOTH hashes so a response is never served
        # across identities (e.g. per Brevitas API key on a shared proxy). Empty =
        # single-tenant local (the default). Costs nothing when unset.
        namespace: str = "",
        # When False, only the exact-hash layer runs (byte-identical repeats) —
        # zero wrong-answer risk, no embedding dependency. Set True to also match
        # reworded-but-equivalent prompts via the semantic layer.
        semantic_enabled: bool = False,
    ):
        if db_path is None:
            db_path = os.getenv("BREVITAS_CACHE_DB") or str(
                Path(__file__).resolve().parent.parent / "api" / "semantic_cache.db"
            )
        self.db_path = db_path
        self.similarity_threshold = similarity_threshold
        self.max_temperature = max_temperature
        self.default_ttl_s = default_ttl_s
        self.namespace = namespace
        self.semantic_enabled = semantic_enabled
        self._init()

    # -- storage ------------------------------------------------------------
    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init(self) -> None:
        with self._conn() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS semantic_cache (
                    exact_hash       TEXT PRIMARY KEY,
                    context_hash     TEXT NOT NULL,
                    model_id         TEXT NOT NULL,
                    embedding        BLOB,
                    response_json    TEXT NOT NULL,
                    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at       REAL NOT NULL,
                    expires_at       REAL NOT NULL,
                    hit_count        INTEGER NOT NULL DEFAULT 0
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS sc_ctx ON semantic_cache (context_hash, expires_at)")
        # Cached responses are conversation content at rest — keep the file private.
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass  # best-effort (e.g. non-local FS); never fail cache init over perms

    # -- keys ---------------------------------------------------------------
    @staticmethod
    def _effective_temp(body: dict) -> float:
        t = body.get("temperature")
        return 0.0 if t is None else float(t)

    def _exact_parts(self, body: dict, provider: str, model: str, *, include_last: bool) -> dict:
        """The request fields that MUST match exactly for a cached answer to be valid.
        With include_last=False the final message is dropped — that's the semantic
        bucket key (everything identical except the question being asked)."""
        request = deepcopy(body)
        namespace = request.pop("_brevitas_cache_namespace", self.namespace)
        messages = request.get("messages", []) or []
        if not include_last and isinstance(messages, list):
            request["messages"] = messages[:-1]
        return {
            "namespace": namespace,
            "provider": provider,
            "model": model,
            # Hash the complete request. Provider APIs keep adding response-affecting
            # controls (seed, stop, reasoning, response_format, modalities, etc.); an
            # allowlist silently reuses the wrong response when a new one appears.
            "request": request,
        }

    @staticmethod
    def _hash(parts: dict) -> str:
        return hashlib.sha256(
            json.dumps(parts, sort_keys=True, default=str).encode()
        ).hexdigest()

    @staticmethod
    def _last_user_text(messages: list) -> str:
        for m in reversed(messages or []):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            content = m.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
        return ""

    # -- policy -------------------------------------------------------------
    @staticmethod
    def _text_only_user_messages(messages: list) -> bool:
        """True only when every user message is plain text (or a list of text blocks).
        Multimodal/structured content (images, audio, tool_use blocks) can differ while
        hashing identically at the text layer, so such requests are never cacheable."""
        for m in messages or []:
            if not isinstance(m, dict):
                return False
            if m.get("role") != "user":
                continue
            content = m.get("content", "")
            if isinstance(content, str):
                continue
            if isinstance(content, list):
                if any(not (isinstance(b, dict) and b.get("type") == "text")
                       for b in content):
                    return False
                continue
            return False           # unknown content shape → not cacheable
        return True

    def cacheable(self, body: dict) -> bool:
        if body.get("stream"):
            return False           # streamed responses handled separately (later)
        if body.get("tools"):
            return False           # tool calls may encode per-request/user args
        # Temperature must be EXPLICITLY 0. An unset or non-zero temperature means the
        # caller may expect fresh sampling, so replaying a stored answer would be wrong.
        temp = body.get("temperature")
        if temp is None or float(temp) != 0.0:
            return False
        if not self._text_only_user_messages(body.get("messages", [])):
            return False           # text-only user content, per the semantic-cache safety rule
        return True

    # -- lookup / store -----------------------------------------------------
    def lookup(self, body: dict, provider: str, model: str) -> CacheHit | None:
        if not self.cacheable(body):
            return None
        now = time.time()
        exact = self._hash(self._exact_parts(body, provider, model, include_last=True))
        with self._conn() as db:
            row = db.execute(
                "SELECT response_json, prompt_tokens, completion_tokens FROM semantic_cache "
                "WHERE exact_hash=? AND expires_at>?",
                (exact, now),
            ).fetchone()
        if row:
            self._bump(exact)
            return CacheHit("exact", json.loads(row[0]), row[1], row[2])

        # Layer 2 — semantic (only if enabled AND embeddings + numpy are available)
        if not self.semantic_enabled or np is None:
            return None
        vec = _embed.embed(self._last_user_text(body.get("messages", [])))
        if vec is None:
            return None
        ctx = self._hash(self._exact_parts(body, provider, model, include_last=False))
        with self._conn() as db:
            rows = db.execute(
                "SELECT exact_hash, response_json, prompt_tokens, completion_tokens, embedding "
                "FROM semantic_cache WHERE context_hash=? AND expires_at>? AND embedding IS NOT NULL",
                (ctx, now),
            ).fetchall()
        best, best_sim = None, -1.0
        for r in rows:
            emb = np.frombuffer(r[4], dtype="float32")
            sim = float(np.dot(vec, emb))       # both normalized → cosine
            if sim > best_sim:
                best, best_sim = r, sim
        if best is not None and best_sim >= self.similarity_threshold:
            self._bump(best[0])
            return CacheHit("semantic", json.loads(best[1]), best[2], best[3], best_sim)
        return None

    def store(self, body: dict, provider: str, model: str, response: dict, *,
              prompt_tokens: int, completion_tokens: int, ttl_s: int | None = None) -> None:
        if not self.cacheable(body):
            return
        now = time.time()
        ttl = self.default_ttl_s if ttl_s is None else ttl_s
        jitter = min(60, max(1, ttl // 10))
        expires = now + ttl + random.randint(-jitter, jitter)  # jitter avoids herd expiry
        exact = self._hash(self._exact_parts(body, provider, model, include_last=True))
        ctx = self._hash(self._exact_parts(body, provider, model, include_last=False))
        vec = (_embed.embed(self._last_user_text(body.get("messages", [])))
               if self.semantic_enabled and np is not None else None)
        emb_bytes = vec.tobytes() if vec is not None else None
        with self._conn() as db:
            db.execute(
                "INSERT OR REPLACE INTO semantic_cache "
                "(exact_hash, context_hash, model_id, embedding, response_json, "
                " prompt_tokens, completion_tokens, created_at, expires_at, hit_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,0)",
                (exact, ctx, f"{provider}:{model}", emb_bytes, json.dumps(response),
                 int(prompt_tokens or 0), int(completion_tokens or 0), now, expires),
            )

    def _bump(self, exact_hash: str) -> None:
        try:
            with self._conn() as db:
                db.execute(
                    "UPDATE semantic_cache SET hit_count=hit_count+1 WHERE exact_hash=?",
                    (exact_hash,),
                )
        except Exception:
            pass  # observability only; never fail a hit over a counter

    def purge(self) -> int:
        """Delete every cached row and return the count removed. Used after a safety
        incident where a row's origin is unknown — some may have been produced from
        retrieval-pruned or lossily-compressed context and must not be replayed."""
        with self._conn() as db:
            n = int(db.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()[0])
            db.execute("DELETE FROM semantic_cache")
        return n


class SupabaseSemanticCache(SemanticCache):
    """Hosted backend so the cache is shared across machines (the SQLite backend is
    per-proxy). Reuses the pure key/policy helpers from SemanticCache; only the DB
    read/write differs — exact lookup is a filtered select, semantic lookup is the
    server-side `semantic_cache_lookup` RPC (cosine in Postgres via pgvector).

    Requires migration 002 applied to the Supabase project. Opt-in only
    (BREVITAS_CACHE_BACKEND=supabase) and NOT yet verified against a live pgvector
    instance — validate before trusting it in production.
    """

    def __init__(self, url: str, service_key: str, *, similarity_threshold: float = 0.97,
                 max_temperature: float = 0.5, default_ttl_s: int = 3600,
                 semantic_enabled: bool = False):
        from supabase import create_client
        self._c = create_client(url, service_key)
        self.similarity_threshold = similarity_threshold
        self.max_temperature = max_temperature
        self.default_ttl_s = default_ttl_s
        self.semantic_enabled = semantic_enabled
        self.namespace = ""
        # NB: no SQLite init — this backend does not touch the local filesystem.

    @staticmethod
    def _vec_literal(vec) -> str:
        return "[" + ",".join(f"{x:.6f}" for x in vec.tolist()) + "]"  # pgvector text form

    def lookup(self, body: dict, provider: str, model: str) -> CacheHit | None:
        if not self.cacheable(body):
            return None
        now_iso = _iso(time.time())
        exact = self._hash(self._exact_parts(body, provider, model, include_last=True))
        try:
            r = (self._c.table("semantic_cache")
                 .select("response_json, prompt_tokens, completion_tokens")
                 .eq("exact_hash", exact).gt("expires_at", now_iso).limit(1).execute())
            if r.data:
                row = r.data[0]
                self._bump(exact)
                return CacheHit("exact", row["response_json"],
                                row["prompt_tokens"], row["completion_tokens"])
            if not self.semantic_enabled or np is None:
                return None
            vec = _embed.embed(self._last_user_text(body.get("messages", [])))
            if vec is None:
                return None
            ctx = self._hash(self._exact_parts(body, provider, model, include_last=False))
            rr = self._c.rpc("semantic_cache_lookup", {
                "p_embedding": self._vec_literal(vec),
                "p_context_hash": ctx,
                "p_threshold": self.similarity_threshold,
            }).execute()
            if rr.data:
                row = rr.data[0]
                self._bump(row["exact_hash"])
                return CacheHit("semantic", row["response_json"], row["prompt_tokens"],
                                row["completion_tokens"], float(row.get("similarity", 1.0)))
        except Exception:
            return None  # cache never breaks the request path
        return None

    def store(self, body: dict, provider: str, model: str, response: dict, *,
              prompt_tokens: int, completion_tokens: int, ttl_s: int | None = None) -> None:
        if not self.cacheable(body):
            return
        now = time.time()
        ttl = self.default_ttl_s if ttl_s is None else ttl_s
        jitter = min(60, max(1, ttl // 10))
        exact = self._hash(self._exact_parts(body, provider, model, include_last=True))
        ctx = self._hash(self._exact_parts(body, provider, model, include_last=False))
        vec = (_embed.embed(self._last_user_text(body.get("messages", [])))
               if self.semantic_enabled and np is not None else None)
        try:
            self._c.table("semantic_cache").upsert({
                "exact_hash": exact, "context_hash": ctx, "model_id": f"{provider}:{model}",
                "embedding": self._vec_literal(vec) if vec is not None else None,
                "response_json": response,
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "created_at": _iso(now),
                "expires_at": _iso(now + ttl + random.randint(-jitter, jitter)),
            }, on_conflict="exact_hash").execute()
        except Exception:
            pass

    def _bump(self, exact_hash: str) -> None:
        try:
            self._c.rpc("increment", {})  # optional; ignore if no such fn
        except Exception:
            pass  # hit-count is observability only

    def purge(self) -> int:
        """Delete every cached row (see SemanticCache.purge). Returns rows removed
        (best-effort — Supabase returns the deleted rows only when configured to)."""
        try:
            # A delete needs a filter; exact_hash is never empty, so this matches all rows.
            res = self._c.table("semantic_cache").delete().neq("exact_hash", "").execute()
            return len(getattr(res, "data", None) or [])
        except Exception:
            return 0


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def make_semantic_cache():
    """Pick the cache backend. Supabase (shared across machines) only when explicitly
    opted in AND service-role creds are present; otherwise the local SQLite backend.
    Any failure falls back to SQLite so the cache is always available."""
    semantic_enabled = os.getenv("BREVITAS_SEMANTIC_CACHE", "false").lower() in (
        "1", "true", "yes")
    if os.getenv("BREVITAS_CACHE_BACKEND", "").lower() == "supabase":
        url = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if url and key:
            try:
                return SupabaseSemanticCache(url, key, semantic_enabled=semantic_enabled)
            except Exception:
                pass
    return SemanticCache(semantic_enabled=semantic_enabled)


def _demo() -> None:
    """Self-check. Run: python -m brevitas.semantic_cache

    Covers exact hit, model isolation, the temp/tools/stream gate, TTL expiry, and
    (only when numpy is available) a semantic hit above / miss below threshold."""
    import tempfile

    db = tempfile.mktemp(suffix=".db")
    c = SemanticCache(db, default_ttl_s=3600)
    resp = {"content": [{"type": "text", "text": "Paris"}]}

    body = {"model": "claude-sonnet-4-6", "temperature": 0,
            "messages": [{"role": "user", "content": "capital of France?"}]}
    assert c.lookup(body, "anthropic", "claude-sonnet-4-6") is None, "cold miss expected"
    c.store(body, "anthropic", "claude-sonnet-4-6", resp, prompt_tokens=10, completion_tokens=1)

    hit = c.lookup(body, "anthropic", "claude-sonnet-4-6")
    assert hit and hit.kind == "exact" and hit.response == resp, "exact hit failed"

    # model isolation: identical text, different model → miss
    assert c.lookup(body, "anthropic", "claude-opus-4-8") is None, "model isolation broken"
    assert c.lookup(body, "openai", "gpt-4o") is None, "provider isolation broken"

    # namespace isolation: same request under a different tenant → miss (no leak)
    other = SemanticCache(db, namespace="tenant-B")
    assert other.lookup(body, "anthropic", "claude-sonnet-4-6") is None, "namespace leak!"
    other.store(body, "anthropic", "claude-sonnet-4-6", {"x": 2}, prompt_tokens=1, completion_tokens=1)
    hitB = other.lookup(body, "anthropic", "claude-sonnet-4-6")
    assert hitB and hitB.response == {"x": 2}, "namespaced store/lookup failed"
    # the original (empty namespace) still sees ITS answer, not tenant-B's
    assert c.lookup(body, "anthropic", "claude-sonnet-4-6").response == resp, "namespaces cross-contaminated"

    # gate: tools / stream / high temp are never cacheable
    assert not c.cacheable({"tools": [{}], "messages": []}), "tools should not cache"
    assert not c.cacheable({"stream": True, "messages": []}), "stream should not cache"
    assert not c.cacheable({"temperature": 0.9, "messages": []}), "high temp should not cache"
    assert c.cacheable({"temperature": 0.0, "messages": []}), "temp 0 should cache"

    # TTL expiry: a row already past expiry is ignored
    c2 = SemanticCache(db, default_ttl_s=-10)  # expires in the past
    b2 = {"model": "m", "temperature": 0, "messages": [{"role": "user", "content": "stale?"}]}
    c2.store(b2, "openai", "m", {"x": 1}, prompt_tokens=1, completion_tokens=1)
    assert c2.lookup(b2, "openai", "m") is None, "expired row must not hit"

    if np is not None:
        # semantic layer with injected fake embeddings (no model download in tests)
        vecs = {
            "how do refunds work": np.array([1.0, 0.0, 0.0], dtype="float32"),
            "what is the refund policy": np.array([0.99, 0.14, 0.0], dtype="float32"),
            "how tall is everest": np.array([0.0, 0.0, 1.0], dtype="float32"),
        }
        for k in vecs:  # normalize so dot == cosine
            vecs[k] /= np.linalg.norm(vecs[k])
        orig = _embed.embed
        _embed.embed = lambda t: vecs.get((t or "").strip().lower())
        try:
            cs = SemanticCache(tempfile.mktemp(suffix=".db"), similarity_threshold=0.97,
                               semantic_enabled=True)
            base = {"model": "claude-sonnet-4-6", "temperature": 0,
                    "messages": [{"role": "user", "content": "how do refunds work"}]}
            cs.store(base, "anthropic", "claude-sonnet-4-6", resp, prompt_tokens=5, completion_tokens=1)

            near = {"model": "claude-sonnet-4-6", "temperature": 0,
                    "messages": [{"role": "user", "content": "what is the refund policy"}]}
            h = cs.lookup(near, "anthropic", "claude-sonnet-4-6")
            assert h and h.kind == "semantic", "reworded query should hit semantically"
            assert h.similarity >= 0.97, h.similarity

            far = {"model": "claude-sonnet-4-6", "temperature": 0,
                   "messages": [{"role": "user", "content": "how tall is everest"}]}
            assert cs.lookup(far, "anthropic", "claude-sonnet-4-6") is None, "unrelated query must miss"
            print("semantic layer ok (reworded hit, unrelated miss)")
        finally:
            _embed.embed = orig
    else:
        print("numpy absent — semantic layer skipped (exact-hash layer verified)")

    print("semantic_cache self-check passed")


if __name__ == "__main__":
    _demo()
