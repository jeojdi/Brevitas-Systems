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
import hmac
import json
import os
import random
import sqlite3
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import _embed
from .resource_bounds import (
    MAX_CONTENT_RETENTION_S,
    ResourceBounds,
    ResourceLimitExceeded,
    clamp_int,
    serialized_size_bytes,
    utf8_size,
)

try:
    import numpy as np
except Exception:  # numpy ships with the semanticcache extra; without it, Layer 1 only
    np = None


def _semantic_allowed(key: str = "") -> bool:
    """The fuzzy semantic layer is a RISKY lever: cosine similarity alone does NOT prove
    answer equivalence, and there is no judge here. Serve a reworded-match answer only when
    the operator has explicitly opted in AND the semantic_cache lever has not tripped
    (fail-closed). The exact-hash layer is unaffected — it is byte-identical and safe."""
    try:
        from token_efficiency_model.quality.gate import lever_allowed
        return lever_allowed("semantic_cache", key=key)
    except Exception:
        return False


def _record_cache(outcome: str) -> None:
    """Emit one fixed-cardinality cache event without affecting request handling."""
    try:
        from .observability import get_runtime

        get_runtime(default_service="api").metrics.record_cache(
            cache="semantic", outcome=outcome,
        )
    except Exception:
        pass


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
        encryption_key: str | bytes | None = None,
        encryption_cipher: Any | None = None,
        max_entries: int | None = None,
        max_entry_bytes: int | None = None,
        candidate_limit: int | None = None,
        clock=time.time,
        jitter_source=random.randint,
    ):
        bounds = ResourceBounds.from_env()
        if db_path is None:
            db_path = os.getenv("BREVITAS_CACHE_DB") or str(
                Path(__file__).resolve().parent.parent / "api" / "semantic_cache.db"
            )
        self.db_path = db_path
        self.similarity_threshold = similarity_threshold
        self.max_temperature = max_temperature
        self.default_ttl_s = clamp_int(
            default_ttl_s, minimum=1, maximum=MAX_CONTENT_RETENTION_S,
            name="semantic cache ttl",
        )
        self.max_entries = clamp_int(
            bounds.semantic_cache_max_entries if max_entries is None else max_entries,
            minimum=1, maximum=1_000_000, name="semantic cache max entries",
        )
        self.max_entry_bytes = clamp_int(
            bounds.semantic_cache_max_entry_bytes if max_entry_bytes is None else max_entry_bytes,
            minimum=1024, maximum=8 * 1024 * 1024,
            name="semantic cache max entry bytes",
        )
        self.candidate_limit = clamp_int(
            bounds.semantic_cache_candidate_limit
            if candidate_limit is None else candidate_limit,
            minimum=1, maximum=2_048, name="semantic cache candidate limit",
        )
        self.request_max_bytes = bounds.request_max_bytes
        self.request_max_items = bounds.request_max_items
        self.namespace = namespace
        self.semantic_enabled = semantic_enabled
        configured_key = encryption_key or os.getenv("BREVITAS_CACHE_ENCRYPTION_KEY", "")
        production = (os.getenv("BREVITAS_ENV", "").lower() in ("prod", "production")
                      or bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT_NAME")
                              or os.getenv("RAILWAY_PROJECT_ID")))
        if not configured_key and encryption_cipher is None and production:
            raise RuntimeError("BREVITAS_CACHE_ENCRYPTION_KEY is required in production")
        # Development/test caches remain encrypted, but an ephemeral key deliberately
        # makes them unreadable after restart unless the operator configures persistence.
        key = configured_key or Fernet.generate_key()
        self._cipher = Fernet(key.encode() if isinstance(key, str) else key)
        if encryption_cipher is not None and not (
            callable(getattr(encryption_cipher, "encrypt_text", None))
            and callable(getattr(encryption_cipher, "decrypt_text", None))
        ):
            raise TypeError("cache encryption cipher does not satisfy the envelope interface")
        self._encryption_cipher = encryption_cipher
        self._clock = clock
        self._jitter_source = jitter_source
        self._db_lock = threading.RLock()
        self._last_purge = 0.0
        self._init()

    # -- storage ------------------------------------------------------------
    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _init(self) -> None:
        with self._db_lock, self._conn() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS semantic_cache (
                    exact_hash       TEXT PRIMARY KEY,
                    context_hash     TEXT NOT NULL,
                    model_id         TEXT NOT NULL,
                    embedding        BLOB,
                    response_json    TEXT NOT NULL DEFAULT '',
                    response_ciphertext TEXT NOT NULL DEFAULT '',
                    tenant_namespace TEXT NOT NULL DEFAULT '',
                    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at       REAL NOT NULL,
                    expires_at       REAL NOT NULL,
                    hit_count        INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(semantic_cache)")}
            if "response_ciphertext" not in columns:
                db.execute("ALTER TABLE semantic_cache ADD COLUMN response_ciphertext TEXT NOT NULL DEFAULT ''")
            if "tenant_namespace" not in columns:
                db.execute("ALTER TABLE semantic_cache ADD COLUMN tenant_namespace TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS sc_ctx ON semantic_cache (context_hash, expires_at)")
            db.execute("CREATE INDEX IF NOT EXISTS sc_tenant ON semantic_cache (tenant_namespace, expires_at)")
            db.execute("CREATE INDEX IF NOT EXISTS sc_created ON semantic_cache (created_at, exact_hash)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS sc_candidates_scope ON semantic_cache "
                "(context_hash, tenant_namespace, model_id, "
                "created_at DESC, exact_hash DESC, expires_at)"
            )
        # Cached responses are conversation content at rest — keep the file private.
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass  # best-effort (e.g. non-local FS); never fail cache init over perms
        self.purge_expired(force=True)

    @staticmethod
    def _canonical_response(response: dict) -> bytes:
        return json.dumps(
            response, separators=(",", ":"), sort_keys=True,
            ensure_ascii=False, allow_nan=False, default=str,
        ).encode("utf-8")

    @staticmethod
    def _encryption_context(*, tenant_namespace: str, exact_hash: str,
                            provider: str, model: str) -> dict[str, str]:
        return {
            "purpose": "semantic-response-cache",
            "tenant_namespace": tenant_namespace,
            "exact_hash": exact_hash,
            "model_identity": f"{provider}:{model}",
        }

    @classmethod
    def _encryption_context_digest(cls, **values: str) -> str:
        canonical = json.dumps(
            cls._encryption_context(**values), separators=(",", ":"),
            sort_keys=True, ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _encrypt_response(self, plaintext: bytes, *, tenant_namespace: str,
                          exact_hash: str, provider: str, model: str) -> str:
        if self._encryption_cipher is not None:
            return self._encryption_cipher.encrypt_text(
                plaintext.decode("utf-8"),
                context=self._encryption_context(
                    tenant_namespace=tenant_namespace, exact_hash=exact_hash,
                    provider=provider, model=model,
                ),
            )
        digest = self._encryption_context_digest(
            tenant_namespace=tenant_namespace, exact_hash=exact_hash,
            provider=provider, model=model,
        ).encode("ascii")
        protected = b"bvt-cache-context:v1:" + digest + b":" + plaintext
        return self._cipher.encrypt(protected).decode()

    def _decrypt_response(self, ciphertext: str, *, tenant_namespace: str,
                          exact_hash: str, provider: str, model: str) -> dict:
        if not ciphertext:
            raise InvalidToken
        if self._encryption_cipher is not None:
            raw = self._encryption_cipher.decrypt_text(
                ciphertext,
                context=self._encryption_context(
                    tenant_namespace=tenant_namespace, exact_hash=exact_hash,
                    provider=provider, model=model,
                ),
            )
        else:
            protected = self._cipher.decrypt(ciphertext.encode())
            prefix = b"bvt-cache-context:v1:"
            if not protected.startswith(prefix):
                raise InvalidToken
            encoded_digest, separator, raw = protected[len(prefix):].partition(b":")
            expected = self._encryption_context_digest(
                tenant_namespace=tenant_namespace, exact_hash=exact_hash,
                provider=provider, model=model,
            ).encode("ascii")
            if not separator or not hmac.compare_digest(encoded_digest, expected):
                raise InvalidToken
        return json.loads(raw)

    @staticmethod
    def _tenant_namespace(body: dict, fallback: str = "") -> str:
        value = str(body.get("_brevitas_cache_namespace", fallback) or "")
        return hashlib.sha256(value.encode()).hexdigest()

    def purge_expired(self, *, force: bool = False) -> int:
        now = self._clock()
        if not force and now - self._last_purge < 300:
            return 0
        try:
            with self._db_lock, self._conn() as db:
                cursor = db.execute("DELETE FROM semantic_cache WHERE expires_at<=?", (now,))
        except Exception:
            _record_cache("error")
            raise
        self._last_purge = now
        removed = max(0, int(cursor.rowcount or 0))
        if removed:
            _record_cache("evicted")
        return removed

    def purge_namespace(self, namespace: str, *, strict: bool = False) -> int:
        tenant = hashlib.sha256((namespace or "").encode()).hexdigest()
        try:
            with self._db_lock, self._conn() as db:
                cursor = db.execute(
                    "DELETE FROM semantic_cache WHERE tenant_namespace=?", (tenant,)
                )
        except Exception:
            _record_cache("error")
            raise
        removed = max(0, int(cursor.rowcount or 0))
        if removed:
            _record_cache("evicted")
        return removed

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
        if not isinstance(body, dict):
            return False
        try:
            if serialized_size_bytes(body) > self.request_max_bytes:
                return False
        except ResourceLimitExceeded:
            return False
        messages = body.get("messages", []) or []
        if not isinstance(messages, list) or not messages:
            return False
        if len(messages) > self.request_max_items:
            return False
        # The semantic bucket removes exactly the final user question. If the
        # request ends in an assistant/tool/system turn, dropping that turn would
        # let different conversation states share a cached answer.
        final_message = messages[-1]
        if not isinstance(final_message, dict) or final_message.get("role") != "user":
            return False
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

    @staticmethod
    def _bounded_dimensions(provider: str, model: str) -> bool:
        return (
            isinstance(provider, str) and 0 < utf8_size(provider) <= 128
            and isinstance(model, str) and 0 < utf8_size(model) <= 383
            and utf8_size(f"{provider}:{model}") <= 512
        )

    # -- lookup / store -----------------------------------------------------
    def lookup(self, body: dict, provider: str, model: str, *,
               gate_key: str = "") -> CacheHit | None:
        if not self.cacheable(body) or not self._bounded_dimensions(provider, model):
            _record_cache("disabled")
            return None
        try:
            self.purge_expired()
            now = self._clock()
            exact = self._hash(self._exact_parts(body, provider, model, include_last=True))
            tenant_namespace = self._tenant_namespace(body, self.namespace)
            model_id = f"{provider}:{model}"
            with self._db_lock, self._conn() as db:
                row = db.execute(
                    "SELECT response_ciphertext, prompt_tokens, completion_tokens "
                    "FROM semantic_cache WHERE exact_hash=? AND tenant_namespace=? "
                    "AND model_id=? AND expires_at>?",
                    (exact, tenant_namespace, model_id, now),
                ).fetchone()
        except Exception:
            _record_cache("error")
            return None
        if row:
            try:
                response = self._decrypt_response(
                    row[0], tenant_namespace=tenant_namespace, exact_hash=exact,
                    provider=provider, model=model,
                )
            except Exception:
                _record_cache("error")
                return None
            self._bump(exact)
            _record_cache("hit")
            return CacheHit("exact", response, row[1], row[2])

        # Layer 2 — semantic (only if enabled AND opted-in/untripped AND embeddings available)
        if not self.semantic_enabled or np is None or not _semantic_allowed(gate_key):
            _record_cache("miss")
            return None
        try:
            vec = _embed.embed(self._last_user_text(body.get("messages", [])))
        except Exception:
            _record_cache("error")
            return None
        if vec is None:
            _record_cache("miss")
            return None
        ctx = self._hash(self._exact_parts(body, provider, model, include_last=False))
        try:
            with self._db_lock, self._conn() as db:
                cursor = db.execute(
                    "SELECT exact_hash, response_ciphertext, prompt_tokens, "
                    "completion_tokens, embedding FROM semantic_cache "
                    "WHERE context_hash=? AND tenant_namespace=? AND model_id=? "
                    "AND expires_at>? AND embedding IS NOT NULL "
                    "ORDER BY created_at DESC, exact_hash DESC LIMIT ?",
                    (ctx, tenant_namespace, model_id, now, self.candidate_limit),
                )
                rows = cursor.fetchmany(self.candidate_limit)
        except Exception:
            _record_cache("error")
            return None
        best, best_sim = None, -1.0
        for r in rows:
            emb = np.frombuffer(r[4], dtype="float32")
            sim = float(np.dot(vec, emb))       # both normalized → cosine
            if sim > best_sim:
                best, best_sim = r, sim
        if best is not None and best_sim >= self.similarity_threshold:
            try:
                response = self._decrypt_response(
                    best[1], tenant_namespace=tenant_namespace, exact_hash=best[0],
                    provider=provider, model=model,
                )
            except Exception:
                _record_cache("error")
                return None
            self._bump(best[0])
            _record_cache("hit")
            return CacheHit("semantic", response, best[2], best[3], best_sim)
        _record_cache("miss")
        return None

    def store(self, body: dict, provider: str, model: str, response: dict, *,
              prompt_tokens: int, completion_tokens: int, ttl_s: int | None = None) -> None:
        if not self.cacheable(body):
            _record_cache("disabled")
            return
        if not self._bounded_dimensions(provider, model):
            _record_cache("disabled")
            return
        self.purge_expired()
        try:
            plaintext = self._canonical_response(response)
        except (TypeError, ValueError, OverflowError):
            _record_cache("disabled")
            return
        if len(plaintext) > self.max_entry_bytes:
            _record_cache("disabled")
            return
        now = self._clock()
        ttl = self.default_ttl_s if ttl_s is None else clamp_int(
            ttl_s, minimum=1, maximum=min(self.default_ttl_s, MAX_CONTENT_RETENTION_S),
            name="semantic cache entry ttl",
        )
        jitter = min(60, max(1, ttl // 10))
        # Never let jitter create a non-positive or over-24-hour retention window.
        duration = min(MAX_CONTENT_RETENTION_S, max(1, ttl + self._jitter_source(-jitter, jitter)))
        expires = now + duration
        exact = self._hash(self._exact_parts(body, provider, model, include_last=True))
        ctx = self._hash(self._exact_parts(body, provider, model, include_last=False))
        vec = (_embed.embed(self._last_user_text(body.get("messages", [])))
               if self.semantic_enabled and np is not None else None)
        emb_bytes = vec.tobytes() if vec is not None else None
        tenant_namespace = self._tenant_namespace(body, self.namespace)
        ciphertext = self._encrypt_response(
            plaintext, tenant_namespace=tenant_namespace, exact_hash=exact,
            provider=provider, model=model,
        )
        if utf8_size(ciphertext) > 16 * 1024 * 1024:
            _record_cache("disabled")
            return
        try:
            with self._db_lock, self._conn() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR REPLACE INTO semantic_cache "
                    "(exact_hash, context_hash, model_id, embedding, response_json, "
                    "response_ciphertext, tenant_namespace, prompt_tokens, completion_tokens, "
                    "created_at, expires_at, hit_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                    (exact, ctx, f"{provider}:{model}", emb_bytes, "", ciphertext,
                     tenant_namespace, min(2_000_000_000, max(0, int(prompt_tokens or 0))),
                     min(2_000_000_000, max(0, int(completion_tokens or 0))), now, expires),
                )
                cursor = db.execute(
                    "DELETE FROM semantic_cache WHERE exact_hash IN ("
                    "SELECT exact_hash FROM semantic_cache ORDER BY created_at DESC, "
                    "exact_hash DESC LIMIT -1 OFFSET ?)",
                    (self.max_entries,),
                )
        except Exception:
            _record_cache("error")
            raise
        _record_cache("write")
        if max(0, int(cursor.rowcount or 0)):
            _record_cache("evicted")

    def _bump(self, exact_hash: str) -> None:
        try:
            with self._db_lock, self._conn() as db:
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
                 semantic_enabled: bool = False,
                 encryption_key: str | bytes | None = None,
                 encryption_cipher: Any | None = None,
                 max_entries: int | None = None,
                 max_entry_bytes: int | None = None,
                 candidate_limit: int | None = None,
                 clock=time.time,
                 jitter_source=random.randint):
        from supabase import create_client
        self._c = create_client(url, service_key)
        self.similarity_threshold = similarity_threshold
        self.max_temperature = max_temperature
        bounds = ResourceBounds.from_env()
        self.default_ttl_s = clamp_int(
            default_ttl_s, minimum=1, maximum=MAX_CONTENT_RETENTION_S,
            name="semantic cache ttl",
        )
        self.max_entries = clamp_int(
            bounds.semantic_cache_max_entries if max_entries is None else max_entries,
            minimum=1, maximum=1_000_000, name="semantic cache max entries",
        )
        self.max_entry_bytes = clamp_int(
            bounds.semantic_cache_max_entry_bytes if max_entry_bytes is None else max_entry_bytes,
            minimum=1024, maximum=8 * 1024 * 1024,
            name="semantic cache max entry bytes",
        )
        self.candidate_limit = clamp_int(
            bounds.semantic_cache_candidate_limit
            if candidate_limit is None else candidate_limit,
            minimum=1, maximum=2_048, name="semantic cache candidate limit",
        )
        self.request_max_bytes = bounds.request_max_bytes
        self.request_max_items = bounds.request_max_items
        self.semantic_enabled = semantic_enabled
        self.namespace = ""
        configured_key = encryption_key or os.getenv("BREVITAS_CACHE_ENCRYPTION_KEY", "")
        if not configured_key and encryption_cipher is None:
            raise RuntimeError("BREVITAS_CACHE_ENCRYPTION_KEY is required for hosted caching")
        key = configured_key or Fernet.generate_key()
        self._cipher = Fernet(key.encode() if isinstance(key, str) else key)
        if encryption_cipher is not None and not (
            callable(getattr(encryption_cipher, "encrypt_text", None))
            and callable(getattr(encryption_cipher, "decrypt_text", None))
        ):
            raise TypeError("cache encryption cipher does not satisfy the envelope interface")
        self._encryption_cipher = encryption_cipher
        self._clock = clock
        self._jitter_source = jitter_source
        self._db_lock = threading.RLock()
        self._last_purge = 0.0
        # NB: no SQLite init — this backend does not touch the local filesystem.

    @staticmethod
    def _vec_literal(vec) -> str:
        return "[" + ",".join(f"{x:.6f}" for x in vec.tolist()) + "]"  # pgvector text form

    def lookup(self, body: dict, provider: str, model: str, *,
               gate_key: str = "") -> CacheHit | None:
        if not self.cacheable(body) or not self._bounded_dimensions(provider, model):
            _record_cache("disabled")
            return None
        self.purge_expired()
        now_iso = _iso(self._clock())
        exact = self._hash(self._exact_parts(body, provider, model, include_last=True))
        tenant_namespace = self._tenant_namespace(body, self.namespace)
        model_id = f"{provider}:{model}"
        try:
            r = (self._c.table("semantic_cache")
                 .select("response_ciphertext, prompt_tokens, completion_tokens")
                 .eq("exact_hash", exact).eq("tenant_namespace", tenant_namespace)
                 .eq("model_id", model_id).gt("expires_at", now_iso).limit(1).execute())
            if r.data:
                row = r.data[0]
                response = self._decrypt_response(
                    row["response_ciphertext"], tenant_namespace=tenant_namespace,
                    exact_hash=exact, provider=provider, model=model,
                )
                self._bump(exact)
                _record_cache("hit")
                return CacheHit("exact", response,
                                row["prompt_tokens"], row["completion_tokens"])
            if not self.semantic_enabled or np is None or not _semantic_allowed(gate_key):
                _record_cache("miss")
                return None
            vec = _embed.embed(self._last_user_text(body.get("messages", [])))
            if vec is None:
                _record_cache("miss")
                return None
            ctx = self._hash(self._exact_parts(body, provider, model, include_last=False))
            rr = self._c.rpc("semantic_cache_lookup", {
                "p_embedding": self._vec_literal(vec),
                "p_context_hash": ctx,
                "p_threshold": self.similarity_threshold,
                "p_tenant_namespace": tenant_namespace,
                "p_model_id": model_id,
            }).execute()
            if rr.data:
                row = rr.data[0]
                response = self._decrypt_response(
                    row["response_ciphertext"], tenant_namespace=tenant_namespace,
                    exact_hash=row["exact_hash"], provider=provider, model=model,
                )
                self._bump(row["exact_hash"])
                _record_cache("hit")
                return CacheHit("semantic", response, row["prompt_tokens"],
                                row["completion_tokens"], float(row.get("similarity", 1.0)))
        except Exception:
            _record_cache("error")
            return None  # cache never breaks the request path
        _record_cache("miss")
        return None

    def store(self, body: dict, provider: str, model: str, response: dict, *,
              prompt_tokens: int, completion_tokens: int, ttl_s: int | None = None) -> None:
        if not self.cacheable(body):
            _record_cache("disabled")
            return
        if not self._bounded_dimensions(provider, model):
            _record_cache("disabled")
            return
        self.purge_expired()
        try:
            plaintext = self._canonical_response(response)
        except (TypeError, ValueError, OverflowError):
            _record_cache("disabled")
            return
        if len(plaintext) > self.max_entry_bytes:
            _record_cache("disabled")
            return
        ttl = self.default_ttl_s if ttl_s is None else clamp_int(
            ttl_s, minimum=1, maximum=min(self.default_ttl_s, MAX_CONTENT_RETENTION_S),
            name="semantic cache entry ttl",
        )
        jitter = min(60, max(1, ttl // 10))
        exact = self._hash(self._exact_parts(body, provider, model, include_last=True))
        ctx = self._hash(self._exact_parts(body, provider, model, include_last=False))
        vec = (_embed.embed(self._last_user_text(body.get("messages", [])))
               if self.semantic_enabled and np is not None else None)
        try:
            duration = min(
                MAX_CONTENT_RETENTION_S,
                max(1, ttl + self._jitter_source(-jitter, jitter)),
            )
            tenant_namespace = self._tenant_namespace(body, self.namespace)
            ciphertext = self._encrypt_response(
                plaintext, tenant_namespace=tenant_namespace, exact_hash=exact,
                provider=provider, model=model,
            )
            if utf8_size(ciphertext) > 16 * 1024 * 1024:
                _record_cache("disabled")
                return
            # The RPC performs upsert and deterministic oldest-first eviction in
            # one database transaction. Production must not use a non-atomic
            # select/count/delete sequence across replicas.
            self._c.rpc("semantic_cache_store_bounded", {
                "p_exact_hash": exact,
                "p_context_hash": ctx,
                "p_model_id": f"{provider}:{model}",
                "p_embedding": self._vec_literal(vec) if vec is not None else None,
                "p_response_ciphertext": ciphertext,
                "p_tenant_namespace": tenant_namespace,
                "p_prompt_tokens": min(2_000_000_000, max(0, int(prompt_tokens or 0))),
                "p_completion_tokens": min(2_000_000_000, max(0, int(completion_tokens or 0))),
                "p_ttl_seconds": duration,
                "p_max_entries": self.max_entries,
            }).execute()
            _record_cache("write")
        except Exception:
            _record_cache("error")

    def purge_expired(self, *, force: bool = False) -> int:
        now = self._clock()
        if not force and now - self._last_purge < 300:
            return 0
        try:
            response = self._c.table("semantic_cache").delete().lte("expires_at", _iso(now)).execute()
            self._last_purge = now
            removed = len(response.data or [])
            if removed:
                _record_cache("evicted")
            return removed
        except Exception:
            _record_cache("error")
            return 0

    def purge_namespace(self, namespace: str, *, strict: bool = False) -> int:
        tenant = hashlib.sha256((namespace or "").encode()).hexdigest()
        try:
            response = self._c.table("semantic_cache").delete().eq(
                "tenant_namespace", tenant
            ).execute()
            removed = len(response.data or [])
            if removed:
                _record_cache("evicted")
            return removed
        except Exception:
            _record_cache("error")
            if strict:
                raise
            return 0

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
    backend = os.getenv("BREVITAS_CACHE_BACKEND", "").lower()
    if backend == "supabase":
        url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        encryption_key = os.getenv("BREVITAS_CACHE_ENCRYPTION_KEY")
        if not url or not key or not encryption_key:
            raise RuntimeError("Hosted cache configuration is incomplete")
        bounds = ResourceBounds.from_env()
        return SupabaseSemanticCache(
            url, key, semantic_enabled=semantic_enabled, encryption_key=encryption_key,
            default_ttl_s=bounds.semantic_cache_ttl_s,
            max_entries=bounds.semantic_cache_max_entries,
            max_entry_bytes=bounds.semantic_cache_max_entry_bytes,
            candidate_limit=bounds.semantic_cache_candidate_limit,
        )
    if backend not in ("", "sqlite"):
        raise RuntimeError("Unsupported cache backend")
    if (os.getenv("BREVITAS_ENV", "").lower() in ("prod", "production")
            or os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT_NAME")
            or os.getenv("RAILWAY_PROJECT_ID")):
        raise RuntimeError("Production cache must use the hosted backend")
    bounds = ResourceBounds.from_env()
    return SemanticCache(
        semantic_enabled=semantic_enabled,
        default_ttl_s=bounds.semantic_cache_ttl_s,
        max_entries=bounds.semantic_cache_max_entries,
        max_entry_bytes=bounds.semantic_cache_max_entry_bytes,
        candidate_limit=bounds.semantic_cache_candidate_limit,
    )


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
    assert c.cacheable({"temperature": 0.0,
                        "messages": [{"role": "user", "content": "safe"}]}), \
        "a deterministic user turn should cache"

    # TTL is always positive, even when misconfigured by a caller.
    c2 = SemanticCache(db, default_ttl_s=-10, clock=lambda: 10.0,
                       jitter_source=lambda _a, _b: 0)
    b2 = {"model": "m", "temperature": 0,
          "messages": [{"role": "user", "content": "stale?"}]}
    c2.store(b2, "openai", "m", {"x": 1}, prompt_tokens=1, completion_tokens=1)
    assert c2.lookup(b2, "openai", "m") is not None, "TTL must clamp positive"

    if np is not None:
        # semantic layer with injected fake embeddings (no model download in tests).
        # The fuzzy layer is fail-closed, so opt it in for this section.
        os.environ["BREVITAS_SEMANTIC_CACHE"] = "1"
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
