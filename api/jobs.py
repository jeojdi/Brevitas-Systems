"""Durable, tenant-scoped asynchronous AI jobs.

Postgres is the source of truth. Redis Streams contains opaque job IDs only and
serves as a low-latency wake-up channel; workers also reclaim expired database
leases so a lost Redis notification or worker crash cannot lose work.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field, field_validator, model_validator

from brevitas.resource_bounds import (
    ResourceBounds,
    ResourceLimitExceeded,
    require_size,
)
from brevitas.security import (
    EnvelopeCipher, EnvelopeError, KMSConfigurationError, KMSUnavailable,
)
from .runtime import hosted_runtime


_SAFE_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TERMINAL = {"succeeded", "failed", "cancelled", "dead"}
_SECRET_FIELDS = {
    "authorization", "api_key", "apikey", "provider_api_key", "secret",
    "token", "x-api-key", "x-brevitas-key",
}


class PermanentJobError(RuntimeError):
    """Safe classification for a job that retries cannot repair."""


class CorruptJobCiphertext(PermanentJobError):
    """A quarantined row whose authenticated ciphertext cannot be opened."""


class JobLeaseLost(RuntimeError):
    """The worker can no longer prove that it owns the durable job."""


def _retryable(exc: Exception) -> bool:
    explicit = getattr(exc, "job_retryable", None)
    if isinstance(explicit, bool):
        return explicit
    if isinstance(exc, (PermanentJobError, ValueError, TypeError)):
        return False
    status = getattr(exc, "status_code", None)
    return status is None or int(status) == 429 or int(status) >= 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owns_active_lease(row: dict, worker_id: str) -> bool:
    if (row.get("lease_owner") != worker_id
            or row.get("status") not in ("leased", "running")):
        return False
    try:
        expires_at = datetime.fromisoformat(str(row.get("lease_expires_at") or ""))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        return False
    return expires_at > datetime.now(timezone.utc)


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in _SECRET_FIELDS or _contains_secret(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


class JobRequest(BaseModel):
    operation: str = Field(default="chat", pattern="^(chat|compress)$")
    task: str = Field(default="", max_length=20_000)
    messages: list[str] = Field(default_factory=list, max_length=256)
    context: list[str] = Field(default_factory=list, max_length=256)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retention_seconds: int = Field(default=3600, ge=60, le=86_400)

    @field_validator("messages", "context")
    @classmethod
    def bound_content(cls, values: list[str]) -> list[str]:
        if any(len(value) > 200_000 for value in values):
            raise ValueError("job content item is too large")
        return values

    @model_validator(mode="after")
    def bound_payload(self):
        payload = self.model_dump()
        if _contains_secret(payload):
            raise ValueError("job payload cannot contain credentials")
        if len(json.dumps(payload).encode()) > 2_000_000:
            raise ValueError("job payload is too large")
        if self.operation == "chat" and not (self.task or self.messages):
            raise ValueError("chat job requires task or messages")
        return self


@dataclass(frozen=True)
class JobTenant:
    organization_id: str
    customer_id: str
    key_hash: str


class JobCrypto:
    """Authenticated job encryption bound to the durable row and tenant.

    The envelope cipher is always injected by the API/worker composition root.
    There is no generated key, plaintext fallback, or environment-held Fernet
    encryption path. An explicitly configured legacy decryptor may open an old
    row once; the returned replacement is immediately persisted by JobService.
    """

    def __init__(self, cipher: EnvelopeCipher, *, bounds: ResourceBounds | None = None):
        if not isinstance(cipher, EnvelopeCipher):
            raise TypeError("JobCrypto requires an EnvelopeCipher")
        self.cipher = cipher
        self.bounds = bounds or ResourceBounds.from_env()

    @staticmethod
    def context(row: dict, field: str) -> dict[str, str]:
        if field not in {"payload", "result"}:
            raise ValueError("invalid job ciphertext field")
        job_id = str(row.get("id") or "")
        organization_id = str(row.get("organization_id") or "")
        if not job_id or not organization_id:
            raise ValueError("job encryption context is incomplete")
        return {
            "purpose": "durable_job",
            "job_id": job_id,
            "organization_id": organization_id,
            "field": field,
        }

    def encrypt(self, value: dict, *, row: dict, field: str) -> str:
        maximum = (self.bounds.job_max_payload_bytes if field == "payload"
                   else self.bounds.job_max_result_bytes)
        require_size(value, maximum, name=f"job {field}")
        encoded = json.dumps(
            value, separators=(",", ":"), sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")
        try:
            return self.cipher.encrypt_bytes(encoded, context=self.context(row, field))
        except KMSUnavailable:
            raise
        except EnvelopeError as exc:
            raise ResourceLimitExceeded(
                f"job {field} exceeds the encryption envelope limit"
            ) from exc

    def decrypt(self, value: str, *, row: dict, field: str) -> tuple[dict, str | None]:
        try:
            decrypted = self.cipher.decrypt_with_metadata(
                value, context=self.context(row, field),
            )
        except KMSUnavailable:
            raise
        except EnvelopeError as exc:
            raise CorruptJobCiphertext("job ciphertext cannot be decrypted") from exc
        maximum = (self.bounds.job_max_payload_bytes if field == "payload"
                   else self.bounds.job_max_result_bytes)
        if len(decrypted.plaintext) > maximum:
            raise CorruptJobCiphertext("job ciphertext exceeds its retained size limit")
        try:
            parsed = json.loads(decrypted.plaintext)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptJobCiphertext("job ciphertext is invalid") from exc
        if not isinstance(parsed, dict):
            raise CorruptJobCiphertext("job ciphertext is invalid")
        replacement = (
            self.encrypt(parsed, row=row, field=field)
            if decrypted.needs_rotation else None
        )
        return parsed, replacement


class InMemoryJobStore:
    """Concurrency-safe test/development store with production-equivalent semantics."""
    def __init__(self, *, bounds: ResourceBounds | None = None):
        self.bounds = bounds or ResourceBounds.from_env()
        self.max_entries = self.bounds.registry_max_entries
        self.max_row_bytes = max(
            self.bounds.registry_max_value_bytes,
            self.bounds.job_max_payload_bytes + self.bounds.job_max_result_bytes,
        )
        self.rows: dict[str, dict] = {}
        self.idempotency: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def _check_row(self, row: dict) -> None:
        require_size(row, self.max_row_bytes, name="in-memory job row")

    def create(self, row: dict) -> tuple[dict, bool]:
        key = (row["organization_id"], row["customer_id"], row["idempotency_key"])
        self._check_row(row)
        with self._lock:
            existing = self.idempotency.get(key)
            if existing:
                return dict(self.rows[existing]), False
            if len(self.rows) >= self.max_entries:
                raise ResourceLimitExceeded("in-memory job store is at capacity")
            self.rows[row["id"]] = dict(row)
            self.idempotency[key] = row["id"]
            return dict(row), True

    def get(self, job_id: str, organization_id: str, customer_id: str) -> dict | None:
        with self._lock:
            row = self.rows.get(job_id)
            if not row or row["organization_id"] != organization_id or row["customer_id"] != customer_id:
                return None
            return dict(row)

    def cancel(self, job_id: str, organization_id: str, customer_id: str) -> dict | None:
        with self._lock:
            row = self.rows.get(job_id)
            if not row or row["organization_id"] != organization_id or row["customer_id"] != customer_id:
                return None
            if row["status"] in _TERMINAL:
                return dict(row)
            row["cancel_requested"] = True
            if row["status"] in ("queued", "leased"):
                row["status"] = "cancelled"
                row["completed_at"] = _now()
            row["updated_at"] = _now()
            return dict(row)

    def claim(self, worker_id: str, lease_seconds: int) -> dict | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            eligible = []
            for row in self.rows.values():
                if (row["status"] == "queued"
                        and datetime.fromisoformat(row["expires_at"]) <= now):
                    row.update(status="dead", completed_at=_now(),
                               last_error_code="expired", updated_at=_now())
                    continue
                lease_expiry = datetime.fromisoformat(row["lease_expires_at"]) if row.get("lease_expires_at") else None
                available = datetime.fromisoformat(row["available_at"])
                reclaim = row["status"] in ("leased", "running") and lease_expiry and lease_expiry < now
                ambiguous_outbound = (
                    row.get("operation") == "chat"
                    and row.get("provider_outbound_started_at") is not None
                    and (row["status"] == "queued" or reclaim)
                )
                if ambiguous_outbound:
                    row.update(
                        status="dead", completed_at=_now(), lease_owner=None,
                        lease_expires_at=None,
                        last_error_code="provider_outcome_ambiguous",
                        updated_at=_now(),
                    )
                    continue
                if reclaim and row["attempts"] >= row["max_attempts"]:
                    row.update(status="dead", completed_at=_now(), lease_owner=None,
                               lease_expires_at=None, last_error_code="lease_expired",
                               updated_at=_now())
                    continue
                if not row["cancel_requested"] and row["attempts"] < row["max_attempts"] and (
                    (row["status"] == "queued" and available <= now) or reclaim
                ):
                    eligible.append(row)
            if not eligible:
                return None
            row = min(eligible, key=lambda item: (item["available_at"], item["created_at"]))
            row.update(status="leased", attempts=row["attempts"] + 1, lease_owner=worker_id,
                       lease_expires_at=(now + timedelta(seconds=lease_seconds)).isoformat(),
                       updated_at=_now())
            return dict(row)

    def renew(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self._lock:
            row = self.rows.get(job_id)
            if not row or not _owns_active_lease(row, worker_id):
                return False
            row["lease_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            ).isoformat()
            row["updated_at"] = _now()
            return True

    def mark_provider_outbound_started(
        self, job_id: str, worker_id: str,
    ) -> dict | None:
        with self._lock:
            row = self.rows.get(job_id)
            if (not row or not _owns_active_lease(row, worker_id)
                    or row.get("status") != "running"
                    or row.get("operation") != "chat"
                    or row.get("cancel_requested")
                    or row.get("provider_outbound_started_at") is not None):
                return None
            row.update(
                provider_outbound_started_at=_now(),
                provider_outbound_attempt=int(row["attempts"]),
                updated_at=_now(),
            )
            self._check_row(row)
            return dict(row)

    def update(self, job_id: str, worker_id: str, values: dict) -> dict | None:
        with self._lock:
            row = self.rows.get(job_id)
            if not row or not _owns_active_lease(row, worker_id):
                return None
            candidate = {**row, **values, "updated_at": _now()}
            self._check_row(candidate)
            row.update(candidate)
            return dict(row)

    def migrate_ciphertext(self, row: dict, field: str, old: str, new: str) -> bool:
        column = f"{field}_ciphertext"
        if column not in {"payload_ciphertext", "result_ciphertext"}:
            return False
        with self._lock:
            current = self.rows.get(str(row.get("id") or ""))
            if not current or current.get(column) != old:
                return False
            candidate = {**current, column: new, "updated_at": _now()}
            self._check_row(candidate)
            current.update(candidate)
            return True

    def quarantine_ciphertext(self, row: dict, worker_id: str) -> bool:
        with self._lock:
            current = self.rows.get(str(row.get("id") or ""))
            if not current or not _owns_active_lease(current, worker_id):
                return False
            current.update(
                status="dead", last_error_code="ciphertext_unreadable",
                completed_at=_now(), lease_owner=None, lease_expires_at=None,
                updated_at=_now(),
            )
            return True

    def quarantine_result(self, row: dict) -> None:
        with self._lock:
            current = self.rows.get(str(row.get("id") or ""))
            if not current or current.get("status") != "succeeded":
                return
            current.update(
                status="dead", last_error_code="ciphertext_unreadable",
                completed_at=_now(), updated_at=_now(),
            )

    def purge(self) -> int:
        now = datetime.now(timezone.utc)
        with self._lock:
            ids = [job_id for job_id, row in self.rows.items()
                   if row["status"] in _TERMINAL and datetime.fromisoformat(row["expires_at"]) <= now]
            for job_id in ids:
                row = self.rows.pop(job_id)
                self.idempotency.pop((row["organization_id"], row["customer_id"], row["idempotency_key"]), None)
            return len(ids)


class SQLiteJobStore:
    """Durable local adapter used by development and offline BVX tests."""
    _COLUMNS = (
        "id", "organization_id", "customer_id", "key_hash", "idempotency_key", "operation",
        "provider", "model", "payload_ciphertext", "result_ciphertext", "status", "attempts",
        "max_attempts", "available_at", "lease_owner", "lease_expires_at", "cancel_requested",
        "provider_outbound_started_at", "provider_outbound_attempt", "last_error_code",
        "created_at", "updated_at", "completed_at", "expires_at",
    )

    def __init__(self, store: Any):
        if not hasattr(store, "_conn"):
            raise TypeError("SQLiteJobStore requires the local store")
        self.store = store
        with self.store._conn() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS ai_jobs (
                    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, customer_id TEXT NOT NULL,
                    key_hash TEXT NOT NULL, idempotency_key TEXT NOT NULL, operation TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                    payload_ciphertext TEXT NOT NULL, result_ciphertext TEXT,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL,
                    available_at TEXT NOT NULL, lease_owner TEXT, lease_expires_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, last_error_code TEXT NOT NULL DEFAULT '',
                    provider_outbound_started_at TEXT, provider_outbound_attempt INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT, expires_at TEXT NOT NULL,
                    UNIQUE(organization_id, customer_id, idempotency_key)
                )
            """)
            columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(ai_jobs)").fetchall()
            }
            if "provider_outbound_started_at" not in columns:
                db.execute(
                    "ALTER TABLE ai_jobs ADD COLUMN provider_outbound_started_at TEXT"
                )
            if "provider_outbound_attempt" not in columns:
                db.execute(
                    "ALTER TABLE ai_jobs ADD COLUMN provider_outbound_attempt INTEGER"
                )

    def _dict(self, row: Any) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        value["cancel_requested"] = bool(value.get("cancel_requested"))
        return value

    def create(self, row: dict) -> tuple[dict, bool]:
        names = list(self._COLUMNS)
        values = [int(bool(row.get(name))) if name == "cancel_requested" else row.get(name)
                  for name in names]
        with self.store._conn() as db:
            try:
                db.execute(
                    f"INSERT INTO ai_jobs ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                    values,
                )
                return dict(row), True
            except Exception as exc:
                if "UNIQUE constraint failed" not in str(exc):
                    raise
                existing = db.execute(
                    "SELECT * FROM ai_jobs WHERE organization_id=? AND customer_id=? AND idempotency_key=?",
                    (row["organization_id"], row["customer_id"], row["idempotency_key"]),
                ).fetchone()
                return self._dict(existing), False

    def get(self, job_id: str, organization_id: str, customer_id: str) -> dict | None:
        with self.store._conn() as db:
            return self._dict(db.execute(
                "SELECT * FROM ai_jobs WHERE id=? AND organization_id=? AND customer_id=?",
                (job_id, organization_id, customer_id),
            ).fetchone())

    def cancel(self, job_id: str, organization_id: str, customer_id: str) -> dict | None:
        with self.store._conn() as db:
            row = self._dict(db.execute(
                "SELECT * FROM ai_jobs WHERE id=? AND organization_id=? AND customer_id=?",
                (job_id, organization_id, customer_id),
            ).fetchone())
            if not row or row["status"] in _TERMINAL:
                return row
            terminal = row["status"] in ("queued", "leased")
            db.execute(
                "UPDATE ai_jobs SET cancel_requested=1,status=?,completed_at=?,updated_at=? "
                "WHERE id=? AND organization_id=? AND customer_id=?",
                ("cancelled" if terminal else row["status"], _now() if terminal else None, _now(),
                 job_id, organization_id, customer_id),
            )
            return self._dict(db.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone())

    def claim(self, worker_id: str, lease_seconds: int) -> dict | None:
        now = datetime.now(timezone.utc)
        with self.store._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE ai_jobs SET status='dead',completed_at=?,updated_at=?,"
                "lease_owner=NULL,lease_expires_at=NULL,"
                "last_error_code='provider_outcome_ambiguous' "
                "WHERE operation='chat' AND provider_outbound_started_at IS NOT NULL "
                "AND (status='queued' OR (status IN ('leased','running') "
                "AND lease_expires_at<?))",
                (_now(), _now(), now.isoformat()),
            )
            db.execute(
                "UPDATE ai_jobs SET status='dead',completed_at=?,updated_at=?,"
                "lease_owner=NULL,lease_expires_at=NULL,last_error_code='lease_expired' "
                "WHERE status IN ('leased','running') AND lease_expires_at<? "
                "AND attempts>=max_attempts",
                (_now(), _now(), now.isoformat()),
            )
            db.execute(
                "UPDATE ai_jobs SET status='dead',completed_at=?,updated_at=?,"
                "last_error_code='expired' WHERE status='queued' AND expires_at<=?",
                (_now(), _now(), now.isoformat()),
            )
            row = db.execute(
                "SELECT * FROM ai_jobs WHERE attempts<max_attempts AND cancel_requested=0 AND "
                "provider_outbound_started_at IS NULL AND "
                "((status='queued' AND available_at<=? AND expires_at>?) OR "
                "(status IN ('leased','running') AND lease_expires_at<?)) "
                "ORDER BY available_at,created_at LIMIT 1",
                (now.isoformat(), now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE ai_jobs SET status='leased',attempts=attempts+1,lease_owner=?,"
                "lease_expires_at=?,updated_at=? WHERE id=?",
                (worker_id, (now + timedelta(seconds=lease_seconds)).isoformat(), _now(), row["id"]),
            )
            return self._dict(db.execute("SELECT * FROM ai_jobs WHERE id=?", (row["id"],)).fetchone())

    def renew(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self.store._conn() as db:
            cursor = db.execute(
                "UPDATE ai_jobs SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND lease_owner=? AND status IN ('leased','running') "
                "AND lease_expires_at>?",
                ((datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(),
                 _now(), job_id, worker_id, _now()),
            )
            return bool(cursor.rowcount)

    def mark_provider_outbound_started(
        self, job_id: str, worker_id: str,
    ) -> dict | None:
        now = _now()
        with self.store._conn() as db:
            cursor = db.execute(
                "UPDATE ai_jobs SET provider_outbound_started_at=?,"
                "provider_outbound_attempt=attempts,updated_at=? "
                "WHERE id=? AND lease_owner=? AND status='running' "
                "AND lease_expires_at>? AND operation='chat' "
                "AND cancel_requested=0 AND provider_outbound_started_at IS NULL",
                (now, now, job_id, worker_id, now),
            )
            if not cursor.rowcount:
                return None
            return self._dict(db.execute(
                "SELECT * FROM ai_jobs WHERE id=?", (job_id,),
            ).fetchone())

    def update(self, job_id: str, worker_id: str, values: dict) -> dict | None:
        allowed = {name: value for name, value in values.items() if name in self._COLUMNS}
        if not allowed:
            return None
        allowed["updated_at"] = _now()
        names = list(allowed)
        params = [int(bool(allowed[name])) if name == "cancel_requested" else allowed[name] for name in names]
        with self.store._conn() as db:
            cursor = db.execute(
                f"UPDATE ai_jobs SET {','.join(f'{name}=?' for name in names)} "
                "WHERE id=? AND lease_owner=? AND status IN ('leased','running') "
                "AND lease_expires_at>?",
                (*params, job_id, worker_id, _now()),
            )
            if not cursor.rowcount:
                return None
            return self._dict(db.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone())

    def purge(self) -> int:
        with self.store._conn() as db:
            cursor = db.execute(
                "DELETE FROM ai_jobs WHERE expires_at<=? AND status IN ('succeeded','failed','cancelled','dead')",
                (_now(),),
            )
            return max(0, int(cursor.rowcount or 0))

    def migrate_ciphertext(self, row: dict, field: str, old: str, new: str) -> bool:
        column = f"{field}_ciphertext"
        if column not in {"payload_ciphertext", "result_ciphertext"}:
            return False
        with self.store._conn() as db:
            cursor = db.execute(
                f"UPDATE ai_jobs SET {column}=?,updated_at=? WHERE id=? "
                f"AND organization_id=? AND customer_id=? AND {column}=?",
                (new, _now(), row["id"], row["organization_id"], row["customer_id"], old),
            )
            return bool(cursor.rowcount)

    def quarantine_ciphertext(self, row: dict, worker_id: str) -> bool:
        with self.store._conn() as db:
            cursor = db.execute(
                "UPDATE ai_jobs SET status='dead',last_error_code='ciphertext_unreadable',"
                "completed_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL "
                "WHERE id=? AND lease_owner=? AND status IN ('leased','running') "
                "AND lease_expires_at>?",
                (_now(), _now(), row["id"], worker_id, _now()),
            )
            return bool(cursor.rowcount)

    def quarantine_result(self, row: dict) -> None:
        with self.store._conn() as db:
            db.execute(
                "UPDATE ai_jobs SET status='dead',last_error_code='ciphertext_unreadable',"
                "completed_at=?,updated_at=? WHERE id=? AND organization_id=? "
                "AND customer_id=? AND status='succeeded'",
                (_now(), _now(), row["id"], row["organization_id"], row["customer_id"]),
            )


class SupabaseJobStore:
    """Job adapter over the authoritative store's service-role PostgREST client."""
    def __init__(self, store: Any):
        if not hasattr(store, "_request"):
            raise TypeError("SupabaseJobStore requires the cloud store")
        self.store = store

    def create(self, row: dict) -> tuple[dict, bool]:
        existing = self._by_idempotency(row)
        if existing:
            return existing, False
        try:
            result = self.store._request("POST", "ai_jobs", data=row) or []
            return (result[0] if result else row), True
        except Exception:
            existing = self._by_idempotency(row)
            if existing:
                return existing, False
            raise

    def _by_idempotency(self, row: dict) -> dict | None:
        rows = self.store._request("GET", "ai_jobs", params={
            "select": "*", "organization_id": f"eq.{row['organization_id']}",
            "customer_id": f"eq.{row['customer_id']}",
            "idempotency_key": f"eq.{row['idempotency_key']}", "limit": "1",
        }) or []
        return rows[0] if rows else None

    def mark_provider_outbound_started(
        self, job_id: str, worker_id: str,
    ) -> dict | None:
        rows = self.store._request(
            "POST", "rpc/mark_ai_job_provider_outbound_started", data={
                "p_job_id": job_id, "p_worker_id": worker_id,
            },
        ) or []
        return rows[0] if isinstance(rows, list) and rows else None

    def renew(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        rows = self.store._request("PATCH", "ai_jobs", params={
            "id": f"eq.{job_id}", "lease_owner": f"eq.{worker_id}",
            "status": "in.(leased,running)",
            "lease_expires_at": f"gt.{_now()}",
        }, data={
            "lease_expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            ).isoformat(),
            "updated_at": _now(),
        }) or []
        return bool(rows)

    def get(self, job_id: str, organization_id: str, customer_id: str) -> dict | None:
        rows = self.store._request("GET", "ai_jobs", params={
            "select": "*", "id": f"eq.{job_id}", "organization_id": f"eq.{organization_id}",
            "customer_id": f"eq.{customer_id}", "limit": "1",
        }) or []
        return rows[0] if rows else None

    def cancel(self, job_id: str, organization_id: str, customer_id: str) -> dict | None:
        row = self.get(job_id, organization_id, customer_id)
        if not row or row["status"] in _TERMINAL:
            return row
        values = {"cancel_requested": True, "updated_at": _now()}
        if row["status"] in ("queued", "leased"):
            values.update(status="cancelled", completed_at=_now())
        rows = self.store._request("PATCH", "ai_jobs", params={
            "id": f"eq.{job_id}", "organization_id": f"eq.{organization_id}",
            "customer_id": f"eq.{customer_id}",
        }, data=values) or []
        return rows[0] if rows else None

    def claim(self, worker_id: str, lease_seconds: int) -> dict | None:
        rows = self.store._request("POST", "rpc/claim_ai_job", data={
            "p_worker_id": worker_id, "p_lease_seconds": lease_seconds,
        }) or []
        return rows[0] if rows else None

    def update(self, job_id: str, worker_id: str, values: dict) -> dict | None:
        rows = self.store._request("PATCH", "ai_jobs", params={
            "id": f"eq.{job_id}", "lease_owner": f"eq.{worker_id}",
            "status": "in.(leased,running)", "lease_expires_at": f"gt.{_now()}",
        }, data={**values, "updated_at": _now()}) or []
        return rows[0] if rows else None

    def purge(self) -> int:
        result = self.store._request("POST", "rpc/purge_expired_ai_jobs", data={})
        if isinstance(result, list):
            result = result[0] if result else 0
        if isinstance(result, dict):
            result = next(iter(result.values()), 0)
        return int(result or 0)

    def migrate_ciphertext(self, row: dict, field: str, old: str, new: str) -> bool:
        column = f"{field}_ciphertext"
        if column not in {"payload_ciphertext", "result_ciphertext"}:
            return False
        current = self.get(row["id"], row["organization_id"], row["customer_id"])
        if not current or current.get(column) != old:
            return False
        rows = self.store._request("PATCH", "ai_jobs", params={
            "id": f"eq.{row['id']}",
            "organization_id": f"eq.{row['organization_id']}",
            "customer_id": f"eq.{row['customer_id']}",
        }, data={column: new, "updated_at": _now()}) or []
        return bool(rows)

    def quarantine_ciphertext(self, row: dict, worker_id: str) -> bool:
        rows = self.store._request("PATCH", "ai_jobs", params={
            "id": f"eq.{row['id']}", "lease_owner": f"eq.{worker_id}",
            "status": "in.(leased,running)", "lease_expires_at": f"gt.{_now()}",
        }, data={
            "status": "dead", "last_error_code": "ciphertext_unreadable",
            "completed_at": _now(), "updated_at": _now(),
            "lease_owner": None, "lease_expires_at": None,
        }) or []
        return bool(rows)

    def quarantine_result(self, row: dict) -> None:
        self.store._request("PATCH", "ai_jobs", params={
            "id": f"eq.{row['id']}",
            "organization_id": f"eq.{row['organization_id']}",
            "customer_id": f"eq.{row['customer_id']}", "status": "eq.succeeded",
        }, data={
            "status": "dead", "last_error_code": "ciphertext_unreadable",
            "completed_at": _now(), "updated_at": _now(),
        })


class RedisJobDispatcher:
    def __init__(self, redis_client: Any | None = None, *,
                 bounds: ResourceBounds | None = None):
        self.redis = redis_client
        self.bounds = bounds or ResourceBounds.from_env()
        self.stream = os.getenv("BREVITAS_JOB_STREAM", "brevitas:jobs")
        if self.redis is None and os.getenv("REDIS_URL"):
            from redis.asyncio import Redis

            self.redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

    async def enqueue(self, job_id: str) -> None:
        if self.redis is None:
            if hosted_runtime():
                raise RuntimeError("Redis is required for production jobs")
            return
        await self.redis.xadd(
            self.stream, {"job_id": job_id},
            maxlen=self.bounds.redis_stream_max_entries, approximate=True,
        )
        await self.redis.expire(self.stream, self.bounds.redis_stream_ttl_s)

    async def wait_for_notification(self, last_id: str = "$", block_ms: int = 1000) -> str:
        """Wait for a Redis wake-up while keeping Postgres as durable truth."""
        if self.redis is None:
            await asyncio.sleep(max(0.05, block_ms / 1000))
            return last_id
        try:
            events = await self.redis.xread({self.stream: last_id}, count=100, block=block_ms)
        except Exception:
            return last_id
        if not events or not events[0][1]:
            return last_id
        return str(events[0][1][-1][0])


Processor = Callable[[dict, dict], dict | Awaitable[dict]]


class JobService:
    def __init__(self, store: Any, *, crypto: JobCrypto | None = None,
                 dispatcher: RedisJobDispatcher | None = None, lease_seconds: int = 180,
                 bounds: ResourceBounds | None = None):
        self.bounds = bounds or ResourceBounds.from_env()
        self.store = store
        self.crypto = crypto
        self.dispatcher = dispatcher or RedisJobDispatcher(bounds=self.bounds)
        self.lease_seconds = max(30, min(3600, lease_seconds))

    def configure_crypto(self, crypto: JobCrypto) -> None:
        if not isinstance(crypto, JobCrypto):
            raise TypeError("invalid job crypto")
        self.crypto = crypto

    def _crypto(self) -> JobCrypto:
        if self.crypto is None:
            raise KMSConfigurationError("durable job encryption is unavailable")
        return self.crypto

    async def submit(self, tenant: JobTenant, request: JobRequest,
                     idempotency_key: str = "") -> tuple[dict, bool]:
        if not tenant.organization_id or not tenant.customer_id:
            raise ValueError("jobs require an attributed customer")
        if not idempotency_key:
            idempotency_key = uuid.uuid4().hex
        if not _SAFE_IDEMPOTENCY.fullmatch(idempotency_key):
            raise ValueError("invalid idempotency key")
        now = datetime.now(timezone.utc)
        job_id = str(uuid.uuid4())
        retention_seconds = min(
            int(request.retention_seconds), self.bounds.job_payload_ttl_s,
            self.bounds.job_result_ttl_s,
        )
        row = {
            "id": job_id, "organization_id": tenant.organization_id,
            "customer_id": tenant.customer_id, "key_hash": tenant.key_hash,
            "idempotency_key": idempotency_key, "operation": request.operation,
            "provider": "", "model": "", "payload_ciphertext": "",
            "result_ciphertext": None, "status": "queued", "attempts": 0,
            "max_attempts": request.max_attempts, "available_at": now.isoformat(),
            "lease_owner": None, "lease_expires_at": None, "cancel_requested": False,
            "provider_outbound_started_at": None, "provider_outbound_attempt": None,
            "last_error_code": "", "created_at": now.isoformat(), "updated_at": now.isoformat(),
            "completed_at": None,
            "expires_at": (now + timedelta(seconds=retention_seconds)).isoformat(),
        }
        row["payload_ciphertext"] = self._crypto().encrypt(
            request.model_dump(), row=row, field="payload",
        )
        created_row, created = await asyncio.to_thread(self.store.create, row)
        if created:
            # The row is already durable. Redis is only a wake-up optimization;
            # losing its notification must not turn an accepted durable job into
            # a client-visible failure or cause a duplicate on retry.
            with contextlib.suppress(Exception):
                await self.dispatcher.enqueue(created_row["id"])
        return self.public(created_row), created

    async def get(self, tenant: JobTenant, job_id: str) -> dict | None:
        row = await asyncio.to_thread(
            self.store.get, job_id, tenant.organization_id, tenant.customer_id
        )
        return self.public(row, include_result=True) if row else None

    async def cancel(self, tenant: JobTenant, job_id: str) -> dict | None:
        row = await asyncio.to_thread(
            self.store.cancel, job_id, tenant.organization_id, tenant.customer_id
        )
        return self.public(row) if row else None

    async def mark_provider_outbound_started(self, row: dict) -> None:
        """Persist the final fence immediately before a billable chat call."""
        worker_id = str(row.get("lease_owner") or "")
        if row.get("operation") != "chat" or not worker_id:
            raise JobLeaseLost("job cannot enter provider outbound state")
        updated = await asyncio.to_thread(
            self.store.mark_provider_outbound_started, row["id"], worker_id,
        )
        if not isinstance(updated, dict):
            raise JobLeaseLost("job lease was lost before provider outbound")
        started_at = updated.get("provider_outbound_started_at")
        try:
            marked_attempt = int(updated.get("provider_outbound_attempt"))
        except (TypeError, ValueError):
            raise JobLeaseLost("provider outbound fence was not persisted") from None
        if not started_at or marked_attempt != int(row.get("attempts") or 0):
            raise JobLeaseLost("provider outbound fence was not persisted")
        row["provider_outbound_started_at"] = started_at
        row["provider_outbound_attempt"] = marked_attempt

    async def process_one(self, worker_id: str, processor: Processor) -> bool:
        """Process one claimed row while its lease remains provably owned.

        Lease fencing prevents a stale worker from committing a result. It does
        not make an already accepted provider request exactly-once: cancellation
        cannot retract an external side effect. Unsupported ambiguous provider
        outcomes must terminalize instead of being sent again; relaxing that
        rule requires verified provider idempotency or reconciliation support.
        """
        row = await asyncio.to_thread(self.store.claim, worker_id, self.lease_seconds)
        if not row:
            return False
        if row.get("cancel_requested"):
            with contextlib.suppress(JobLeaseLost):
                await self._owned_update(row["id"], worker_id, {
                    "status": "cancelled", "completed_at": _now(),
                    "lease_owner": None, "lease_expires_at": None,
                })
            return True
        try:
            await self._owned_update(row["id"], worker_id, {"status": "running"})
        except JobLeaseLost:
            return True
        heartbeat = asyncio.create_task(self._renew_lease(row["id"], worker_id))
        try:
            payload, replacement = await asyncio.to_thread(
                self._crypto().decrypt, row["payload_ciphertext"],
                row=row, field="payload",
            )
            if heartbeat.done():
                await heartbeat
            if replacement is not None:
                migrated = await asyncio.to_thread(
                    self.store.update, row["id"], worker_id,
                    {"payload_ciphertext": replacement},
                )
                if not migrated:
                    raise JobLeaseLost("job lease was lost during ciphertext migration")
            result = await self._run_processor_with_lease(
                processor, payload, row, heartbeat,
            )
            if not isinstance(result, dict):
                raise TypeError("job processor must return an object")
            metadata = result.pop("_job_metadata", {})
            current = await asyncio.to_thread(
                self.store.get, row["id"], row["organization_id"], row["customer_id"]
            )
            cancelled = bool(current and current.get("cancel_requested"))
            if cancelled:
                values = {"status": "cancelled", "result_ciphertext": None}
            else:
                result_ciphertext = await asyncio.to_thread(
                    self._crypto().encrypt, result or {}, row=row, field="result",
                )
                if heartbeat.done():
                    await heartbeat
                values = {
                    "status": "succeeded", "result_ciphertext": result_ciphertext,
                }
            if isinstance(metadata, dict):
                values["provider"] = str(metadata.get("provider") or "")[:64]
                values["model"] = str(metadata.get("model") or "")[:128]
            await self._confirm_lease(row["id"], worker_id, heartbeat)
            values.update(
                completed_at=_now(), lease_owner=None, lease_expires_at=None,
                last_error_code="",
            )
            await self._owned_update(row["id"], worker_id, values)
        except JobLeaseLost:
            pass
        except CorruptJobCiphertext:
            try:
                await self._confirm_lease(row["id"], worker_id, heartbeat)
                quarantined = await asyncio.to_thread(
                    self.store.quarantine_ciphertext, row, worker_id,
                )
                if not quarantined:
                    raise JobLeaseLost("job lease was lost before quarantine")
            except JobLeaseLost:
                pass
        except Exception as exc:
            retryable = _retryable(exc)
            outbound_started = row.get("provider_outbound_started_at") is not None
            definitely_not_accepted = bool(getattr(
                exc, "provider_outbound_not_accepted", False,
            ))
            ambiguous_outbound = outbound_started and not definitely_not_accepted
            can_retry = (
                not ambiguous_outbound and retryable
                and int(row["attempts"]) < int(row["max_attempts"])
            )
            backoff = min(300, 2 ** min(8, int(row["attempts"]))) + secrets.randbelow(3)
            try:
                await self._confirm_lease(row["id"], worker_id, heartbeat)
                values = {
                    "status": (
                        "dead" if ambiguous_outbound else
                        "queued" if can_retry else
                        "failed" if not retryable else "dead"
                    ),
                    "available_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=backoff)
                    ).isoformat(),
                    "lease_expires_at": None, "lease_owner": None,
                    "last_error_code": (
                        "provider_outcome_ambiguous" if ambiguous_outbound
                        else type(exc).__name__[:64]
                    ),
                    "completed_at": None if can_retry else _now(),
                }
                if outbound_started and definitely_not_accepted:
                    values.update(
                        provider_outbound_started_at=None,
                        provider_outbound_attempt=None,
                    )
                    row["provider_outbound_started_at"] = None
                    row["provider_outbound_attempt"] = None
                await self._owned_update(row["id"], worker_id, values)
            except JobLeaseLost:
                pass
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, JobLeaseLost):
                await heartbeat
        return True

    async def _owned_update(
        self, job_id: str, worker_id: str, values: dict,
    ) -> dict:
        updated = await asyncio.to_thread(
            self.store.update, job_id, worker_id, values,
        )
        if not updated:
            raise JobLeaseLost("job lease ownership check failed")
        return updated

    async def _confirm_lease(
        self, job_id: str, worker_id: str, heartbeat: asyncio.Task,
    ) -> None:
        if heartbeat.done():
            await heartbeat
        try:
            renewed = await asyncio.to_thread(
                self.store.renew, job_id, worker_id, self.lease_seconds,
            )
        except Exception as exc:
            raise JobLeaseLost("job lease renewal failed") from exc
        if not renewed:
            raise JobLeaseLost("job lease renewal was rejected")
        if heartbeat.done():
            await heartbeat

    @staticmethod
    async def _invoke_processor(
        processor: Processor, payload: dict, row: dict,
    ) -> dict:
        if inspect.iscoroutinefunction(processor):
            return await processor(payload, row)
        result = await asyncio.to_thread(processor, payload, row)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _run_processor_with_lease(
        self, processor: Processor, payload: dict, row: dict,
        heartbeat: asyncio.Task,
    ) -> dict:
        if heartbeat.done():
            await heartbeat
        processor_task = asyncio.create_task(
            self._invoke_processor(processor, payload, row),
        )
        try:
            done, _ = await asyncio.wait(
                {processor_task, heartbeat}, return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                await heartbeat
                raise JobLeaseLost("job lease heartbeat stopped")
            return await processor_task
        finally:
            if not processor_task.done():
                processor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await processor_task

    async def _renew_lease(self, job_id: str, worker_id: str) -> None:
        interval = max(0.05, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await asyncio.to_thread(
                    self.store.renew, job_id, worker_id, self.lease_seconds
                )
            except Exception as exc:
                raise JobLeaseLost("job lease renewal failed") from exc
            if not renewed:
                raise JobLeaseLost("job lease renewal was rejected")

    def public(self, row: dict, *, include_result: bool = False) -> dict:
        result = {
            key: row.get(key) for key in (
                "id", "status", "operation", "provider", "model", "attempts", "max_attempts",
                "cancel_requested", "last_error_code", "created_at", "updated_at", "completed_at",
                "expires_at",
            )
        }
        if include_result and row.get("status") == "succeeded" and row.get("result_ciphertext"):
            try:
                decoded, replacement = self._crypto().decrypt(
                    row["result_ciphertext"], row=row, field="result",
                )
            except CorruptJobCiphertext:
                self.store.quarantine_result(row)
                result.update(status="dead", last_error_code="ciphertext_unreadable")
                return result
            if replacement is not None:
                self.store.migrate_ciphertext(
                    row, "result", row["result_ciphertext"], replacement,
                )
            result["result"] = decoded
        return result
