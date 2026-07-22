"""Authoritative usage stores: Supabase in cloud, SQLite for offline work/tests."""
from __future__ import annotations

import atexit
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
import requests

from .auth import generate_api_key, hash_key
from .runtime import hosted_runtime
from brevitas.receipts import MODEL_PRICES, canonical_provider, model_price


USAGE_PAGE_DEFAULT = 100
USAGE_PAGE_MAX = 200
USAGE_BATCH_MAX = 100
LOCAL_USAGE_SCAN_MAX = 10_000
ADMIN_RESULT_MAX = 500
ADMIN_SORT_FIELDS = frozenset({
    "actual_cost_usd", "baseline_cost_usd", "verified_savings_usd",
    "brevitas_fee_usd", "calls", "tokens_saved",
})
AUDIT_ROLES = frozenset({
    "company_owner", "company_admin", "member", "billing_admin",
    "brevitas_admin", "service_account", "system", "legacy", "none",
})
ATOMIC_DASHBOARD_KEY_ROLES = frozenset({
    "company_owner", "company_admin", "member", "billing_admin",
})
DASHBOARD_SESSION_PER_ACTOR_CAP = 8
DASHBOARD_SESSION_PER_COMPANY_CAP = 1000
COMPANY_ROLES = frozenset({
    "company_owner", "company_admin", "member", "billing_admin",
})
_DASHBOARD_KEY_FIELDS = frozenset({
    "id", "name", "created", "key_type", "scopes", "environment", "prefix",
    "service_account_id", "expires_at", "last_used_at", "revoked_at",
})
_DEVICE_RECEIPT_FIELDS = frozenset({
    "status", "already_consumed", "device_hash", "key_hash", "encrypted_key",
    "owner_id", "organization_id", "consumed_at",
})
_ONBOARDING_STATUS_FIELDS = frozenset({
    "company_id", "status", "cli_connected", "proxied_request_observed",
    "completed_at",
})
_AUDIT_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class AmbiguousUsageBatchError(RuntimeError):
    """Raised when a batch may have committed and cannot be retried idempotently."""

    def __init__(self, message: str, records: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.records = [dict(record) for record in (records or [])]


class UsageBatchPartialFailure(RuntimeError):
    """Raised when a store reports an incomplete batch result."""

    def __init__(self, message: str, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.result = dict(result or {})


PROVIDER_COSTS_PER_1M: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
for (provider, model), prices in MODEL_PRICES.items():
    PROVIDER_COSTS_PER_1M[provider][model] = dict(prices)
PROVIDER_COSTS_PER_1M = dict(PROVIDER_COSTS_PER_1M)


def infer_provider(model: str, given: str = "") -> str:
    return canonical_provider(given, model)


def cost_for_tokens(provider: str, model: str, tokens: int) -> float:
    price = model_price(provider, model)
    return 0.0 if not price else max(0, tokens) * price["input"] / 1_000_000


_USAGE_COLUMNS: dict[str, str] = {
    "owner_id": "TEXT NOT NULL DEFAULT ''",
    "organization_id": "TEXT NOT NULL DEFAULT ''",
    "customer_id": "TEXT NOT NULL DEFAULT ''",
    "authoritative": "INTEGER NOT NULL DEFAULT 0",
    "project": "TEXT NOT NULL DEFAULT 'Unattributed'",
    "environment": "TEXT NOT NULL DEFAULT 'Unattributed'",
    "source": "TEXT NOT NULL DEFAULT 'Unattributed'",
    "repo": "TEXT NOT NULL DEFAULT ''",
    "client": "TEXT NOT NULL DEFAULT ''",
    "agent": "TEXT NOT NULL DEFAULT ''",
    "call_site_id": "TEXT NOT NULL DEFAULT ''",
    "framework": "TEXT NOT NULL DEFAULT ''",
    "gateway": "TEXT NOT NULL DEFAULT ''",
    "operation": "TEXT NOT NULL DEFAULT 'chat'",
    "provider": "TEXT NOT NULL DEFAULT ''",
    "model": "TEXT NOT NULL DEFAULT ''",
    "baseline_tokens": "INTEGER NOT NULL DEFAULT 0",
    "optimized_tokens": "INTEGER NOT NULL DEFAULT 0",
    "tokens_saved": "INTEGER NOT NULL DEFAULT 0",
    "savings_pct": "REAL NOT NULL DEFAULT 0",
    "fresh_input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_5m_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_1h_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_attributable": "INTEGER NOT NULL DEFAULT 0",
    "output_tokens": "INTEGER NOT NULL DEFAULT 0",
    "baseline_cost_usd": "REAL",
    "actual_cost_usd": "REAL",
    "measured_savings_usd": "REAL",
    "provider_input_tokens_avoided": "INTEGER NOT NULL DEFAULT 0",
    "native_cache_discount_usd": "REAL",
    "calls_avoided": "INTEGER NOT NULL DEFAULT 0",
    "transport_bytes_avoided": "INTEGER NOT NULL DEFAULT 0",
    "brevitas_incremental_savings_usd": "REAL",
    "verified_savings_usd": "REAL NOT NULL DEFAULT 0",
    "cost_saved_usd": "REAL NOT NULL DEFAULT 0",
    "brevitas_fee_usd": "REAL NOT NULL DEFAULT 0",
    "quality_proxy": "REAL",
    "quality_status": "TEXT NOT NULL DEFAULT ''",
    "pricing_status": "TEXT NOT NULL DEFAULT 'unpriced'",
    "pricing_version": "TEXT NOT NULL DEFAULT ''",
    "strategy": "TEXT NOT NULL DEFAULT ''",
    "receipt_source": "TEXT NOT NULL DEFAULT 'sdk'",
    "is_stream": "INTEGER NOT NULL DEFAULT 0",
    "session_id": "TEXT NOT NULL DEFAULT ''",
    "pipeline": "TEXT NOT NULL DEFAULT ''",
    "run_id": "TEXT NOT NULL DEFAULT ''",
    "request_id": "TEXT NOT NULL DEFAULT ''",
    "usage_raw": "TEXT NOT NULL DEFAULT ''",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_identity(actor_id: str, request_id: str,
                    actor_role: str) -> tuple[str, str, str]:
    resolved_actor = str(actor_id or "system")
    resolved_request = str(request_id or f"store:{uuid.uuid4().hex}")
    resolved_role = str(actor_role or "legacy")
    if not _AUDIT_REQUEST_ID.fullmatch(resolved_request):
        raise ValueError("invalid audit request_id")
    if resolved_role not in AUDIT_ROLES:
        raise ValueError("invalid audit actor_role")
    if (not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", resolved_actor)
            or re.fullmatch(r"[0-9a-fA-F]{64}", resolved_actor)):
        raise ValueError("invalid audit actor_id")
    return resolved_actor, resolved_request, resolved_role


def _required_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid {field}") from exc


def _canonical_store_company_role(value: Any) -> str:
    role = str(value or "")
    return {"owner": "company_owner", "admin": "company_admin",
            "billing": "billing_admin"}.get(role, role)


def _dashboard_expiry(value: str) -> str:
    now = datetime.now(timezone.utc)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid dashboard key expiry") from exc
        if parsed.tzinfo is None:
            raise ValueError("dashboard key expiry must include timezone")
        expiry = parsed.astimezone(timezone.utc)
    else:
        expiry = now + timedelta(hours=8)
    if expiry <= now or expiry > now + timedelta(hours=8):
        raise ValueError("dashboard key expiry must be within 8 hours")
    return expiry.isoformat()


def _rpc_object(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    return dict(value) if isinstance(value, dict) else {}


def _validated_onboarding_status(value: Any, organization_id: str) -> dict[str, Any]:
    """Accept only the content-free, tenant-bound onboarding RPC contract."""
    status = _rpc_object(value)
    if status.pop("ok", False) is not True or set(status) != _ONBOARDING_STATUS_FIELDS:
        raise RuntimeError("onboarding status RPC returned an unsafe response")
    try:
        returned_organization = _required_uuid(
            str(status.get("company_id") or ""), "onboarding company_id")
        expected_organization = _required_uuid(
            organization_id, "expected onboarding company_id")
    except ValueError as exc:
        raise RuntimeError("onboarding status RPC returned an unsafe tenant") from exc
    if returned_organization != expected_organization:
        raise RuntimeError("onboarding status RPC returned the wrong tenant")
    if status.get("status") not in ("pending", "complete"):
        raise RuntimeError("onboarding status RPC returned an invalid state")
    if not isinstance(status.get("cli_connected"), bool) or not isinstance(
            status.get("proxied_request_observed"), bool):
        raise RuntimeError("onboarding status RPC returned invalid evidence")
    completed_at = str(status.get("completed_at") or "")
    if (status["status"] == "complete") != bool(completed_at):
        raise RuntimeError("onboarding status RPC returned inconsistent completion")
    if completed_at:
        try:
            parsed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError("onboarding status RPC returned an invalid timestamp") from exc
        if parsed.tzinfo is None:
            raise RuntimeError("onboarding status RPC returned a naive timestamp")
    return {
        **status,
        "company_id": returned_organization,
        "completed_at": completed_at,
    }


def _validated_installation_registration(
        value: Any, installation_id: str) -> dict[str, Any]:
    """Accept only the bounded response from the atomic registration RPC."""
    response = _rpc_object(value)
    if response.pop("ok", False) is not True or set(response) != {
            "id", "last_seen_at", "device_authorization_bound"}:
        raise RuntimeError("installation registration RPC returned an unsafe response")
    try:
        returned_id = _required_uuid(
            str(response.get("id") or ""), "registered installation_id")
        expected_id = _required_uuid(
            installation_id, "expected installation_id")
    except ValueError as exc:
        raise RuntimeError(
            "installation registration RPC returned an invalid identity") from exc
    if returned_id != expected_id:
        raise RuntimeError("installation registration RPC returned the wrong installation")
    if not isinstance(response.get("device_authorization_bound"), bool):
        raise RuntimeError("installation registration RPC returned invalid authorization state")
    last_seen_at = str(response.get("last_seen_at") or "")
    try:
        parsed = datetime.fromisoformat(last_seen_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(
            "installation registration RPC returned an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("installation registration RPC returned a naive timestamp")
    return {
        "id": returned_id,
        "last_seen_at": last_seen_at,
        "device_authorization_bound": response["device_authorization_bound"],
    }


def _device_consume_identity(device_hash: str, expected_key_hash: str,
                             request_id: str) -> tuple[str, str, str]:
    device_digest = str(device_hash or "")
    key_digest = str(expected_key_hash or "")
    consume_request_id = str(request_id or "")
    if not _SHA256_DIGEST.fullmatch(device_digest):
        raise ValueError("invalid device digest")
    if not _SHA256_DIGEST.fullmatch(key_digest):
        raise ValueError("invalid device key digest")
    if not _AUDIT_REQUEST_ID.fullmatch(consume_request_id):
        raise ValueError("invalid device consume request_id")
    return device_digest, key_digest, consume_request_id


def _validated_device_receipt(value: dict[str, Any], *, device_hash: str,
                              expected_key_hash: str) -> dict[str, Any]:
    receipt = dict(value)
    if set(receipt) != _DEVICE_RECEIPT_FIELDS:
        raise RuntimeError("device consume RPC returned unsafe receipt")
    if receipt.get("status") != "consumed" or not isinstance(
            receipt.get("already_consumed"), bool):
        raise RuntimeError("device consume RPC returned invalid status")
    if not hmac.compare_digest(str(receipt.get("device_hash") or ""), device_hash):
        raise RuntimeError("device consume RPC returned wrong device")
    if not hmac.compare_digest(str(receipt.get("key_hash") or ""), expected_key_hash):
        raise RuntimeError("device consume RPC returned wrong key digest")
    if not isinstance(receipt.get("encrypted_key"), str) or not receipt["encrypted_key"]:
        raise RuntimeError("device consume RPC returned missing ciphertext")
    if not isinstance(receipt.get("owner_id"), str) or not receipt["owner_id"]:
        raise RuntimeError("device consume RPC returned missing owner")
    if not isinstance(receipt.get("organization_id"), str) or not receipt["organization_id"]:
        raise RuntimeError("device consume RPC returned missing tenant")
    try:
        consumed_at = datetime.fromisoformat(
            str(receipt.get("consumed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise RuntimeError("device consume RPC returned invalid timestamp") from exc
    if consumed_at.tzinfo is None:
        raise RuntimeError("device consume RPC returned naive timestamp")
    return receipt


def _credential_unexpired(expires_at: Any, now: str) -> bool:
    if not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        current = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return bool(expiry.tzinfo and current.tzinfo and expiry > current)


def _append_sqlite_device_denial_audit(
        db: sqlite3.Connection, *, organization_id: str, request_id: str,
        target_type: str, target_id: str) -> None:
    """Append one opaque/content-free denial in the caller's mutation transaction."""
    if target_type not in ("api_key", "device_receipt"):
        raise ValueError("invalid device denial target type")
    opaque_target = _required_uuid(target_id, "device denial target_id")
    if not _AUDIT_REQUEST_ID.fullmatch(str(request_id or "")):
        raise ValueError("invalid device denial request_id")
    db.execute(
        "INSERT INTO audit_events(organization_id,actor_user_id,action,target_type,"
        "target_id,details,occurred_at,request_id,actor_id,actor_role,outcome) "
        "VALUES(?,'','device_key.consume.denied',?,?, '{}',?,?,'system','system','denied')",
        (str(organization_id or ""), target_type, opaque_target, _now(), request_id),
    )


def _record_postgres_dependency(outcome: str, duration_seconds: float) -> None:
    """Emit fixed-cardinality dependency telemetry without affecting store behavior."""
    try:
        from brevitas.observability import get_runtime
        get_runtime(default_service="api").metrics.record_dependency(
            dependency="postgres", outcome=outcome,
            duration_seconds=max(0.0, float(duration_seconds)),
        )
    except Exception:
        # Metrics are never part of the authoritative database outcome.
        return


def _cursor_secret(value: str) -> bytes:
    raw = str(value or "").encode()
    if len(raw) < 32:
        raise RuntimeError("COMPANY_ADMIN_CURSOR_SECRET must be at least 32 characters")
    return raw


def _encode_dashboard_cursor(secret: bytes, organization_id: str,
                             timestamp: str, row_id: str) -> str:
    payload = json.dumps({"v": 1, "c": organization_id, "k": "dashboard_keys",
                          "t": timestamp, "i": row_id},
                         separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")


def _decode_dashboard_cursor(secret: bytes, cursor: str,
                             organization_id: str) -> tuple[str, str] | None:
    if not cursor:
        return None
    if len(cursor) > 512:
        raise ValueError("invalid dashboard key cursor")
    try:
        packed = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        if len(packed) <= 32:
            raise ValueError
        payload, signature = packed[:-32], packed[-32:]
        if not hmac.compare_digest(
            signature, hmac.new(secret, payload, hashlib.sha256).digest()
        ):
            raise ValueError
        value = json.loads(payload)
        if value != {"v": 1, "c": organization_id, "k": "dashboard_keys",
                     "t": value.get("t"), "i": value.get("i")}:
            raise ValueError
        timestamp = str(value["t"])
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        row_id = _required_uuid(str(value["i"]), "cursor key_id")
        return timestamp, row_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid dashboard key cursor") from exc


def _dashboard_key_tuple(row: dict[str, Any]) -> tuple[datetime, int]:
    if set(row) != _DASHBOARD_KEY_FIELDS:
        raise RuntimeError("dashboard key RPC returned unsafe metadata")
    row["id"] = _required_uuid(str(row.get("id") or ""), "dashboard key id")
    try:
        created = datetime.fromisoformat(str(row.get("created") or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("dashboard key RPC returned invalid timestamp") from exc
    if created.tzinfo is None:
        raise RuntimeError("dashboard key RPC returned naive timestamp")
    if not isinstance(row.get("scopes"), list) or not all(
        isinstance(scope, str) for scope in row["scopes"]
    ):
        raise RuntimeError("dashboard key RPC returned invalid scopes")
    service_account_id = row.get("service_account_id")
    if service_account_id is not None:
        row["service_account_id"] = _required_uuid(
            str(service_account_id), "service_account_id",
        )
    for field in ("expires_at", "last_used_at", "revoked_at"):
        if row.get(field):
            try:
                timestamp = datetime.fromisoformat(str(row[field]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise RuntimeError(f"dashboard key RPC returned invalid {field}") from exc
            if timestamp.tzinfo is None:
                raise RuntimeError(f"dashboard key RPC returned naive {field}")
    return created.astimezone(timezone.utc), uuid.UUID(row["id"]).int


def _bounded_limit(limit: int, maximum: int = USAGE_PAGE_MAX) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = USAGE_PAGE_DEFAULT
    return max(1, min(value, maximum))


def _encode_usage_cursor(ts: str, row_id: int) -> str:
    raw = json.dumps({"ts": str(ts), "id": int(row_id)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_usage_cursor(cursor: str) -> tuple[str, int] | None:
    if not cursor:
        return None
    if len(cursor) > 512:
        raise ValueError("usage cursor is too long")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        ts, row_id = str(value["ts"]), int(value["id"])
        if not ts or row_id < 1:
            raise ValueError
        return ts, row_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid usage cursor") from exc


def _admin_row_key(row: dict[str, Any]) -> str:
    labels = ("account_id", "repo", "environment", "client", "agent",
              "call_site_id", "framework", "gateway", "provider", "model",
              "operation", "project", "source")
    raw = "\x1f".join(str(row.get(label) or "") for label in labels)
    return hashlib.sha256(raw.encode()).hexdigest()


def _encode_admin_cursor(sort: str, direction: str, value: Any, row_key: str) -> str:
    raw = json.dumps({"sort": sort, "direction": direction, "value": str(value or 0),
                      "key": row_key}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_admin_cursor(cursor: str, sort: str,
                         direction: str) -> tuple[str, str] | None:
    if not cursor:
        return None
    if len(cursor) > 512:
        raise ValueError("admin cursor is too long")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if value["sort"] != sort or value["direction"] != direction:
            raise ValueError
        sort_value, row_key = str(value["value"]), str(value["key"])
        if not row_key:
            raise ValueError
        return sort_value, row_key
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("admin cursor does not match sort order") from exc


def _definite_noncommit(exc: requests.RequestException) -> bool:
    response = getattr(exc, "response", None)
    return bool(response is not None and 400 <= response.status_code < 500)


def _batch_record_row(record: dict[str, Any]) -> dict[str, Any]:
    values = dict(record)
    try:
        key_hash = values.pop("key_hash")
        baseline_tokens = values.pop("baseline_tokens")
        optimized_tokens = values.pop("optimized_tokens")
    except KeyError as exc:
        raise ValueError(f"missing usage batch field: {exc.args[0]}") from exc
    savings_pct = values.pop("savings_pct", 0)
    quality_proxy = values.pop("quality_proxy", None)
    return _usage_row(str(key_hash), int(baseline_tokens), int(optimized_tokens),
                      float(savings_pct or 0), quality_proxy, **values)


def _usage_row(key_hash: str, baseline_tokens: int, optimized_tokens: int,
               savings_pct: float = 0, quality_proxy: Optional[float] = None,
               **values: Any) -> dict[str, Any]:
    project = values.get("project") or values.get("repo") or values.get("pipeline") or "Unattributed"
    source = values.get("source") or values.get("client") or "Unattributed"
    saved = int(values.get("tokens_saved", baseline_tokens - optimized_tokens))
    pct = (100 * saved / baseline_tokens) if baseline_tokens else 0.0
    verified = values.get("verified_savings_usd")
    if verified is None:
        verified = values.get("cost_saved_usd", 0.0)
    row = {
        "key_hash": key_hash,
        "owner_id": values.get("owner_id", ""),
        "organization_id": values.get("organization_id", ""),
        "customer_id": values.get("customer_id", ""),
        "authoritative": bool(values.get("authoritative")),
        "ts": values.get("ts") or _now(),
        "project": project[:128],
        "environment": (values.get("environment") or "Unattributed")[:64],
        "source": source[:128],
        "repo": (values.get("repo") or project)[:128],
        "client": (values.get("client") or source)[:128],
        "agent": (values.get("agent") or "")[:128],
        "call_site_id": (values.get("call_site_id") or "")[:128],
        "framework": (values.get("framework") or "")[:64],
        "gateway": (values.get("gateway") or "")[:64],
        "operation": (values.get("operation") or "chat")[:64],
        "provider": (values.get("provider") or "")[:64],
        "model": (values.get("model") or "")[:128],
        "baseline_tokens": int(baseline_tokens),
        "optimized_tokens": int(optimized_tokens),
        "tokens_saved": saved,
        "savings_pct": round(float(savings_pct if savings_pct else pct), 4),
        "fresh_input_tokens": int(values.get("fresh_input_tokens") or 0),
        "cached_input_tokens": int(values.get("cached_input_tokens") or values.get("cached_tokens") or 0),
        "cache_write_tokens": int(values.get("cache_write_tokens") or 0),
        "cache_write_5m_tokens": int(values.get("cache_write_5m_tokens") or 0),
        "cache_write_1h_tokens": int(values.get("cache_write_1h_tokens") or 0),
        "cache_attributable": bool(values.get("cache_attributable")),
        "output_tokens": int(values.get("output_tokens") or 0),
        "baseline_cost_usd": values.get("baseline_cost_usd"),
        "actual_cost_usd": values.get("actual_cost_usd"),
        "measured_savings_usd": values.get("measured_savings_usd"),
        "provider_input_tokens_avoided": int(
            values.get("provider_input_tokens_avoided") or 0),
        "native_cache_discount_usd": values.get("native_cache_discount_usd"),
        "calls_avoided": int(values.get("calls_avoided") or 0),
        "transport_bytes_avoided": int(values.get("transport_bytes_avoided") or 0),
        "brevitas_incremental_savings_usd": values.get(
            "brevitas_incremental_savings_usd"),
        "verified_savings_usd": round(float(verified or 0), 10),
        "cost_saved_usd": round(float(verified or 0), 10),
        "brevitas_fee_usd": round(float(values.get("brevitas_fee_usd") or 0), 10),
        "quality_proxy": round(float(quality_proxy), 6) if quality_proxy is not None else None,
        "quality_status": values.get("quality_status") or "",
        "pricing_status": values.get("pricing_status") or "unpriced",
        "pricing_version": values.get("pricing_version") or "",
        "strategy": values.get("strategy") or "",
        "receipt_source": values.get("receipt_source") or "sdk",
        "is_stream": bool(values.get("is_stream")),
        "session_id": values.get("session_id") or "",
        "pipeline": values.get("pipeline") or "",
        "run_id": values.get("run_id") or "",
        "request_id": values.get("request_id") or "",
        # Legacy column retained for schema compatibility; raw provider JSON is not persisted.
        "usage_raw": "",
    }
    return row


def _f(value: Any) -> float:
    return float(value or 0)


def _i(value: Any) -> int:
    return int(value or 0)


def _utc_week_start(value: Any) -> str:
    """Return the Monday UTC bucket used by the Postgres dashboard query."""
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return (parsed - timedelta(days=parsed.weekday())).date().isoformat()


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = sum(_i(r.get("baseline_tokens")) for r in rows)
    optimized = sum(_i(r.get("optimized_tokens")) for r in rows)
    saved = sum(_i(r.get("tokens_saved")) for r in rows)
    measured = sum(_f(r.get("measured_savings_usd")) for r in rows)
    input_avoided = sum(_i(r.get("provider_input_tokens_avoided")) for r in rows)
    native_discount = sum(_f(r.get("native_cache_discount_usd")) for r in rows)
    calls_avoided = sum(_i(r.get("calls_avoided")) for r in rows)
    transport_avoided = sum(_i(r.get("transport_bytes_avoided")) for r in rows)
    incremental_rows = [r for r in rows if r.get("brevitas_incremental_savings_usd") is not None]
    incremental = sum(_f(r.get("brevitas_incremental_savings_usd"))
                      for r in incremental_rows)
    verified = sum(_f(r.get("verified_savings_usd", r.get("cost_saved_usd"))) for r in rows)
    actual_cost = sum(_f(r.get("actual_cost_usd")) for r in rows)
    baseline_cost = sum(_f(r.get("baseline_cost_usd")) for r in rows)
    fee = sum(_f(r.get("brevitas_fee_usd")) for r in rows)
    quality = [float(r["quality_proxy"]) for r in rows if r.get("quality_proxy") is not None]
    weeks: dict[str, dict[str, Any]] = {}
    for row in rows:
        week_start = _utc_week_start(row.get("ts"))
        bucket = weeks.setdefault(week_start, {
            "week_start": week_start, "calls": 0, "tokens_saved": 0,
            "provider_input_tokens_avoided": 0, "calls_avoided": 0,
            "native_cache_discount_usd": 0.0,
            "transport_bytes_avoided": 0,
            "actual_cost_usd": 0.0,
            "measured_savings_usd": 0.0, "verified_savings_usd": 0.0,
            "cost_saved_usd": 0.0, "brevitas_fee_usd": 0.0})
        bucket["calls"] += 1
        bucket["tokens_saved"] += _i(row.get("tokens_saved"))
        bucket["provider_input_tokens_avoided"] += _i(
            row.get("provider_input_tokens_avoided"))
        bucket["calls_avoided"] += _i(row.get("calls_avoided"))
        bucket["native_cache_discount_usd"] += _f(
            row.get("native_cache_discount_usd"))
        bucket["transport_bytes_avoided"] += _i(
            row.get("transport_bytes_avoided"))
        bucket["actual_cost_usd"] += _f(row.get("actual_cost_usd"))
        bucket["measured_savings_usd"] += _f(row.get("measured_savings_usd"))
        v = _f(row.get("verified_savings_usd", row.get("cost_saved_usd")))
        bucket["verified_savings_usd"] += v
        bucket["cost_saved_usd"] += v
        bucket["brevitas_fee_usd"] += _f(row.get("brevitas_fee_usd"))
    history = sorted(rows, key=lambda r: str(r.get("ts") or ""), reverse=True)[:50]
    return {
        "total_calls": len(rows),
        "total_baseline_tokens": baseline,
        "total_optimized_tokens": optimized,
        "total_actual_tokens": sum(_i(r.get(k)) for r in rows for k in
                                   ("fresh_input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")),
        "total_tokens_saved": saved,
        "avg_savings_pct": round(100 * saved / baseline, 2) if baseline else 0.0,
        "avg_quality_proxy": round(sum(quality) / len(quality), 4) if quality else 0.0,
        "total_baseline_cost_usd": round(baseline_cost, 8),
        "total_actual_cost_usd": round(actual_cost, 8),
        "total_measured_savings_usd": round(measured, 8),
        "total_provider_input_tokens_avoided": input_avoided,
        "total_native_cache_discount_usd": round(native_discount, 8),
        "total_calls_avoided": calls_avoided,
        "total_transport_bytes_avoided": transport_avoided,
        "total_brevitas_incremental_savings_usd": (
            round(incremental, 8) if incremental_rows else None),
        "incremental_control_calls": len(incremental_rows),
        "total_verified_savings_usd": round(verified, 8),
        "total_cost_saved_usd": round(verified, 8),
        "total_brevitas_fee_usd": round(fee, 8),
        "unpriced_calls": sum(1 for r in rows if r.get("pricing_status") != "priced"),
        "history": [{
            "timestamp": r.get("ts"), "baseline_tokens": _i(r.get("baseline_tokens")),
            "optimized_tokens": _i(r.get("optimized_tokens")),
            "savings_pct": _f(r.get("savings_pct")), "quality_proxy": r.get("quality_proxy"),
            "project": r.get("project") or "Unattributed", "environment": r.get("environment") or "Unattributed",
            "source": r.get("source") or "Unattributed", "provider": r.get("provider") or "",
            "model": r.get("model") or "", "operation": r.get("operation") or "",
            "measured_savings_usd": r.get("measured_savings_usd"),
            "provider_input_tokens_avoided": _i(
                r.get("provider_input_tokens_avoided")),
            "native_cache_discount_usd": r.get("native_cache_discount_usd"),
            "calls_avoided": _i(r.get("calls_avoided")),
            "transport_bytes_avoided": _i(r.get("transport_bytes_avoided")),
            "brevitas_incremental_savings_usd": r.get(
                "brevitas_incremental_savings_usd"),
            "verified_savings_usd": _f(r.get("verified_savings_usd", r.get("cost_saved_usd"))),
            "cost_saved_usd": _f(r.get("verified_savings_usd", r.get("cost_saved_usd"))),
            "pricing_status": r.get("pricing_status") or "unpriced",
        } for r in history],
        "billing_by_week": [weeks[k] for k in sorted(weeks, reverse=True)[:12]],
    }


_BREAKDOWN_FIELDS = ("repo", "environment", "client", "agent",
                     "call_site_id", "framework", "gateway", "provider", "model", "operation")


def _breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        labels = {
            "repo": row.get("repo") or row.get("project") or "Unattributed",
            "environment": row.get("environment") or "Unattributed",
            "client": row.get("client") or row.get("source") or "Unattributed",
        }
        groups[tuple(labels.get(f) or row.get(f) or "" for f in _BREAKDOWN_FIELDS)].append(row)
    out = []
    for key, items in groups.items():
        stat = _stats(items)
        labels = dict(zip(_BREAKDOWN_FIELDS, key))
        out.append({**labels,
                    "project": items[0].get("project") or labels["repo"],
                    "source": items[0].get("source") or labels["client"],
                    "calls": stat["total_calls"],
                    "baseline_tokens": stat["total_baseline_tokens"],
                    "optimized_tokens": stat["total_optimized_tokens"],
                    "actual_tokens": stat["total_actual_tokens"],
                    "tokens_saved": stat["total_tokens_saved"],
                    "baseline_cost_usd": stat["total_baseline_cost_usd"],
                    "actual_cost_usd": stat["total_actual_cost_usd"],
                    "measured_savings_usd": stat["total_measured_savings_usd"],
                    "provider_input_tokens_avoided": stat[
                        "total_provider_input_tokens_avoided"],
                    "native_cache_discount_usd": stat[
                        "total_native_cache_discount_usd"],
                    "calls_avoided": stat["total_calls_avoided"],
                    "transport_bytes_avoided": stat[
                        "total_transport_bytes_avoided"],
                    "brevitas_incremental_savings_usd": stat[
                        "total_brevitas_incremental_savings_usd"],
                    "verified_savings_usd": stat["total_verified_savings_usd"],
                    "brevitas_fee_usd": stat["total_brevitas_fee_usd"],
                    "unpriced_calls": stat["unpriced_calls"]})
    return sorted(out, key=lambda r: (-r["tokens_saved"], r["repo"], r["client"], r["model"]))


def _admin_breakdown(rows: list[dict[str, Any]], emails: dict[str, str] | None = None) -> list[dict[str, Any]]:
    emails = emails or {}
    accounts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        accounts[str(row.get("owner_id") or "Unattributed")].append(row)
    return [{"account_id": account, "account_email": emails.get(account, ""), **item}
            for account, account_rows in accounts.items()
            for item in _breakdown(account_rows)]


def _admin_key_inventory(keys: list[dict[str, Any]], repositories: list[dict[str, Any]],
                         usage: list[dict[str, Any]], emails: dict[str, str] | None = None) -> dict[str, Any]:
    """Build an admin-safe key-to-repository view without exposing raw credentials."""
    emails = emails or {}
    repos_by_key: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    def add_repo(key_hash: str, name: str, installed_at: str = "", last_seen: str = "",
                 source: str = "usage") -> None:
        name = str(name or "").strip()
        if not key_hash or not name:
            return
        current = repos_by_key[key_hash].get(name, {})
        repos_by_key[key_hash][name] = {
            "name": name,
            "source": current.get("source") or source,
            "installed_at": current.get("installed_at") or installed_at,
            "last_seen": max(str(current.get("last_seen") or ""), str(last_seen or installed_at or "")),
        }

    for row in repositories:
        add_repo(str(row.get("key_hash") or ""), str(row.get("repo") or ""),
                 str(row.get("installed_at") or ""), str(row.get("last_seen") or ""),
                 str(row.get("source") or "bvx"))
    for row in usage:
        add_repo(str(row.get("key_hash") or ""),
                 str(row.get("repo") or row.get("project") or ""),
                 last_seen=str(row.get("ts") or ""))

    records = []
    for key in keys:
        key_hash = str(key.get("key_hash") or "")
        owner_id = str(key.get("owner_id") or "")
        records.append({
            "account_id": owner_id or "Unattributed",
            "account_email": emails.get(owner_id, ""),
            "key_id": key_hash[:12],
            "key_name": str(key.get("name") or "unnamed"),
            "created": str(key.get("created") or ""),
            "repositories": sorted(repos_by_key.get(key_hash, {}).values(), key=lambda row: row["name"].lower()),
        })
    records.sort(key=lambda row: (row["account_email"] or row["account_id"], row["created"]), reverse=True)
    return {"keys": records, "total_keys": len(records),
            "total_repositories": sum(len(row["repositories"]) for row in records)}


def _filter_admin_rows(rows: list[dict[str, Any]], filters: dict[str, str]) -> list[dict[str, Any]]:
    start = filters.get("start", "")
    result = []
    for row in rows:
        if start and str(row.get("ts") or "") < start:
            continue
        if any(filters.get(field) and str(row.get(field) or "") != filters[field]
               for field in ("owner_id", "project", "client", "provider", "model")):
            continue
        result.append(row)
    return result


class UsageStore:
    """SQLite development/test fallback with the same public methods as Supabase."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(Path(__file__).parent / "brevitas.db")
        self._init()

    def _conn(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def healthy(self) -> bool:
        with self._conn() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1

    def _init(self) -> None:
        with self._conn() as db:
            db.execute("CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT '')")
            key_cols = {r[1] for r in db.execute("PRAGMA table_info(api_keys)")}
            if "owner_id" not in key_cols:
                db.execute("ALTER TABLE api_keys ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            key_upgrades = {
                "id": "TEXT NOT NULL DEFAULT ''",
                "organization_id": "TEXT NOT NULL DEFAULT ''",
                "service_account_id": "TEXT NOT NULL DEFAULT ''",
                "key_type": "TEXT NOT NULL DEFAULT 'legacy'",
                "scopes": "TEXT NOT NULL DEFAULT 'proxy:invoke,usage:write,usage:read_own,repositories:register'",
                "environment": "TEXT NOT NULL DEFAULT ''",
                "key_prefix": "TEXT NOT NULL DEFAULT ''",
                "expires_at": "TEXT NOT NULL DEFAULT ''",
                "last_used_at": "TEXT NOT NULL DEFAULT ''",
                "revoked_at": "TEXT NOT NULL DEFAULT ''",
                "created_by": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in key_upgrades.items():
                if name not in key_cols:
                    db.execute(f"ALTER TABLE api_keys ADD COLUMN {name} {definition}")
            for row in db.execute("SELECT key_hash FROM api_keys WHERE id='' OR id IS NULL").fetchall():
                db.execute("UPDATE api_keys SET id=? WHERE key_hash=?", (str(uuid.uuid4()), row[0]))
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS api_keys_id_idx ON api_keys(id)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS api_keys_dashboard_sessions_idx "
                "ON api_keys(organization_id,created_by,created,id) "
                "WHERE key_type='dashboard_session'"
            )
            db.execute("CREATE TABLE IF NOT EXISTS organizations (id TEXT PRIMARY KEY, name TEXT NOT NULL, legacy_owner_id TEXT NOT NULL DEFAULT '' UNIQUE, account_type TEXT NOT NULL DEFAULT 'company', cache_enabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
            organization_cols = {r[1] for r in db.execute("PRAGMA table_info(organizations)")}
            if "billing_owner_id" not in organization_cols:
                db.execute("ALTER TABLE organizations ADD COLUMN billing_owner_id TEXT NOT NULL DEFAULT ''")
            if "account_type" not in organization_cols:
                db.execute("ALTER TABLE organizations ADD COLUMN account_type TEXT NOT NULL DEFAULT 'company'")
            for name, definition in {
                "onboarding_started_at": "TEXT NOT NULL DEFAULT ''",
                "onboarding_completed_at": "TEXT NOT NULL DEFAULT ''",
                "onboarding_completed_by": "TEXT NOT NULL DEFAULT ''",
                "onboarding_evidence_usage_id": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in organization_cols:
                    db.execute(f"ALTER TABLE organizations ADD COLUMN {name} {definition}")
            db.execute(
                "UPDATE organizations SET onboarding_started_at=created_at "
                "WHERE onboarding_started_at=''"
            )
            db.execute("UPDATE organizations SET billing_owner_id=legacy_owner_id WHERE billing_owner_id='' AND legacy_owner_id<>''")
            db.execute("CREATE TABLE IF NOT EXISTS organization_members (organization_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, PRIMARY KEY(organization_id,user_id))")
            db.execute("CREATE TABLE IF NOT EXISTS customers (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, external_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', cache_enabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(organization_id,external_id))")
            if "cache_enabled" not in {r[1] for r in db.execute("PRAGMA table_info(organizations)")}:
                db.execute("ALTER TABLE organizations ADD COLUMN cache_enabled INTEGER NOT NULL DEFAULT 0")
            if "cache_enabled" not in {r[1] for r in db.execute("PRAGMA table_info(customers)")}:
                db.execute("ALTER TABLE customers ADD COLUMN cache_enabled INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS service_accounts (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL, environment TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(organization_id,name,environment))")
            service_account_cols = {
                r[1] for r in db.execute("PRAGMA table_info(service_accounts)")
            }
            for name, definition in {
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "expires_at": "TEXT NOT NULL DEFAULT ''",
                "revoked_at": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in service_account_cols:
                    db.execute(
                        f"ALTER TABLE service_accounts ADD COLUMN {name} {definition}"
                    )
            db.execute("CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, device_fingerprint TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT '', UNIQUE(organization_id,device_fingerprint))")
            db.execute("CREATE TABLE IF NOT EXISTS installations (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, device_id TEXT NOT NULL DEFAULT '', service_account_id TEXT NOT NULL DEFAULT '', repository_id TEXT NOT NULL DEFAULT '', repository TEXT NOT NULL DEFAULT '', environment TEXT NOT NULL DEFAULT '', device_platform TEXT NOT NULL DEFAULT '', device_arch TEXT NOT NULL DEFAULT '', client_name TEXT NOT NULL DEFAULT '', bvx_version TEXT NOT NULL DEFAULT '', installed_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT '')")
            installation_cols = {r[1] for r in db.execute("PRAGMA table_info(installations)")}
            for name in ("repository_id", "device_platform", "device_arch", "client_name"):
                if name not in installation_cols:
                    db.execute(f"ALTER TABLE installations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            for name in (
                "registration_key_hash", "registration_key_id", "device_auth_receipt_id",
            ):
                if name not in installation_cols:
                    db.execute(
                        f"ALTER TABLE installations ADD COLUMN {name} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL DEFAULT '', actor_user_id TEXT NOT NULL DEFAULT '', actor_key_hash TEXT DEFAULT NULL, action TEXT NOT NULL, target_type TEXT NOT NULL DEFAULT '', target_id TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '{}', occurred_at TEXT NOT NULL)")
            audit_columns = {r[1] for r in db.execute("PRAGMA table_info(audit_events)")}
            audit_upgrades = {
                "request_id": "TEXT NOT NULL DEFAULT 'legacy-store'",
                "actor_id": "TEXT NOT NULL DEFAULT 'system'",
                "actor_role": "TEXT NOT NULL DEFAULT 'legacy'",
                "outcome": "TEXT NOT NULL DEFAULT 'committed'",
            }
            for name, definition in audit_upgrades.items():
                if name not in audit_columns:
                    db.execute(f"ALTER TABLE audit_events ADD COLUMN {name} {definition}")
            db.execute("CREATE TABLE IF NOT EXISTS provider_config (key_hash TEXT PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'ollama', provider_api_key TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT 'llama3.2')")
            db.execute("""CREATE TRIGGER IF NOT EXISTS provider_config_revoke_cleanup
                AFTER UPDATE OF revoked_at ON api_keys
                WHEN NEW.revoked_at IS NOT NULL AND NEW.revoked_at<>''
                BEGIN
                    DELETE FROM provider_config WHERE key_hash=NEW.key_hash;
                END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS provider_config_delete_cleanup
                AFTER DELETE ON api_keys
                BEGIN
                    DELETE FROM provider_config WHERE key_hash=OLD.key_hash;
                END""")
            db.execute("CREATE TABLE IF NOT EXISTS bvx_device_auth (device_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT '', key_hash TEXT NOT NULL DEFAULT '', encrypted_key TEXT NOT NULL DEFAULT '', approved_at TEXT NOT NULL DEFAULT '')")
            db.execute("CREATE TABLE IF NOT EXISTS key_repositories (key_hash TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT '', repo TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'bvx', installed_at TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY (key_hash, repo))")
            device_cols = {r[1] for r in db.execute("PRAGMA table_info(bvx_device_auth)")}
            if "key_hash" not in device_cols:
                db.execute("ALTER TABLE bvx_device_auth ADD COLUMN key_hash TEXT NOT NULL DEFAULT ''")
            if "organization_id" not in device_cols:
                db.execute("ALTER TABLE bvx_device_auth ADD COLUMN organization_id TEXT NOT NULL DEFAULT ''")
            if "quarantined_at" not in device_cols:
                db.execute("ALTER TABLE bvx_device_auth ADD COLUMN quarantined_at TEXT NOT NULL DEFAULT ''")
            db.execute(
                "CREATE TABLE IF NOT EXISTS bvx_device_consumption_receipts ("
                "device_hash TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL, "
                "encrypted_key TEXT NOT NULL, owner_id TEXT NOT NULL, "
                "approver_id TEXT NOT NULL DEFAULT '', "
                "organization_id TEXT NOT NULL, consumed_at TEXT NOT NULL, "
                "expires_at TEXT NOT NULL, request_id TEXT NOT NULL, "
                "quarantined_at TEXT NOT NULL DEFAULT '')"
            )
            receipt_cols = {
                r[1] for r in db.execute(
                    "PRAGMA table_info(bvx_device_consumption_receipts)")
            }
            if "quarantined_at" not in receipt_cols:
                db.execute(
                    "ALTER TABLE bvx_device_consumption_receipts ADD COLUMN "
                    "quarantined_at TEXT NOT NULL DEFAULT ''"
                )
            if "approver_id" not in receipt_cols:
                db.execute(
                    "ALTER TABLE bvx_device_consumption_receipts ADD COLUMN "
                    "approver_id TEXT NOT NULL DEFAULT ''"
                )
            if "id" not in receipt_cols:
                db.execute(
                    "ALTER TABLE bvx_device_consumption_receipts ADD COLUMN "
                    "id TEXT NOT NULL DEFAULT ''"
                )
                for row in db.execute(
                        "SELECT device_hash FROM bvx_device_consumption_receipts "
                        "WHERE id='' OR id IS NULL").fetchall():
                    db.execute(
                        "UPDATE bvx_device_consumption_receipts SET id=? WHERE device_hash=?",
                        (str(uuid.uuid4()), row[0]),
                    )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS bvx_device_receipt_id_idx "
                "ON bvx_device_consumption_receipts(id)"
            )
            db.execute(
                "UPDATE bvx_device_consumption_receipts SET encrypted_key='',"
                "quarantined_at=? WHERE approver_id='' AND quarantined_at=''",
                (_now(),),
            )
            definitions = ",\n".join(f"{name} {definition}" for name, definition in _USAGE_COLUMNS.items())
            db.execute(f"CREATE TABLE IF NOT EXISTS usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT NOT NULL, ts TEXT NOT NULL, {definitions})")
            existing = {r[1] for r in db.execute("PRAGMA table_info(usage_log)")}
            for name, definition in _USAGE_COLUMNS.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE usage_log ADD COLUMN {name} {definition}")
            # Early databases required quality_proxy even though modern lossless calls
            # intentionally record it as NULL. Rebuild once so those calls are not silently
            # dropped by SQLite's INSERT OR IGNORE.
            quality_column = next(
                r for r in db.execute("PRAGMA table_info(usage_log)") if r[1] == "quality_proxy"
            )
            if quality_column[3]:
                columns = ["id", "key_hash", "ts", *_USAGE_COLUMNS]
                names = ",".join(columns)
                db.execute("ALTER TABLE usage_log RENAME TO usage_log_legacy")
                db.execute(
                    f"CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    f"key_hash TEXT NOT NULL, ts TEXT NOT NULL, {definitions})"
                )
                db.execute(f"INSERT INTO usage_log ({names}) SELECT {names} FROM usage_log_legacy")
                db.execute("DROP TABLE usage_log_legacy")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS usage_request_unique ON usage_log(key_hash, request_id) WHERE request_id <> ''")
            db.execute("CREATE INDEX IF NOT EXISTS usage_key_page_idx ON usage_log(key_hash, ts DESC, id DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_owner_page_idx ON usage_log(owner_id, ts DESC, id DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_org_page_idx ON usage_log(organization_id, ts DESC, id DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_org_customer_page_idx ON usage_log(organization_id, customer_id, ts DESC, id DESC)")
            for column in ("project", "client", "provider", "model", "pipeline", "agent", "run_id"):
                db.execute(f"CREATE INDEX IF NOT EXISTS usage_org_{column}_idx ON usage_log(organization_id, {column}, ts DESC, id DESC)")
            db.execute("UPDATE usage_log SET measured_savings_usd=cost_saved_usd, verified_savings_usd=cost_saved_usd WHERE measured_savings_usd IS NULL")

    def ensure_organization(self, user_id: str, name: str = "",
                            account_type: str = "company") -> dict[str, Any]:
        """Idempotently create the human member's default organization."""
        if account_type not in {"individual", "company"}:
            raise ValueError("invalid account type")
        with self._conn() as db:
            row = db.execute(
                "SELECT o.id,o.name,o.billing_owner_id,o.account_type FROM organizations o JOIN organization_members m ON m.organization_id=o.id WHERE m.user_id=? ORDER BY m.created_at LIMIT 1",
                (user_id,),
            ).fetchone()
            if not row:
                organization_id = str(uuid.uuid4())
                now = _now()
                db.execute("INSERT INTO organizations(id,name,legacy_owner_id,billing_owner_id,account_type,created_at,onboarding_started_at) VALUES(?,?,?,?,?,?,?)",
                           (organization_id, name or "My organization", user_id, user_id,
                            account_type, now, now))
                db.execute("INSERT INTO organization_members(organization_id,user_id,role,created_at) VALUES(?,?,?,?)",
                           (organization_id, user_id, "owner", now))
                row = db.execute("SELECT id,name,billing_owner_id,account_type FROM organizations WHERE id=?", (organization_id,)).fetchone()
        return {"id": row[0], "name": row[1], "billing_owner_id": row[2],
                "account_type": row[3]}

    def member_organization(self, user_id: str) -> dict[str, Any] | None:
        with self._conn() as db:
            member_columns = {
                row[1] for row in db.execute("PRAGMA table_info(organization_members)")
            }
            active_clause = " AND m.status='active'" if "status" in member_columns else ""
            selection_exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='active_company_selections'",
            ).fetchone()
            row = None
            if selection_exists and "status" in member_columns:
                # Serialize fallback and switch resolution so a stale preference
                # can never authorize a disabled/removed membership.
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT o.id,o.name,m.role,o.billing_owner_id,o.account_type FROM "
                    "active_company_selections selected "
                    "JOIN organization_members m ON m.user_id=selected.user_id "
                    "AND m.organization_id=selected.organization_id "
                    "JOIN organizations o ON o.id=m.organization_id "
                    "WHERE selected.user_id=? AND m.status='active' AND m.role IN "
                    "('owner','admin','billing','company_owner','company_admin','member','billing_admin')",
                    (user_id,),
                ).fetchone()
                if not row:
                    row = db.execute(
                        "SELECT o.id,o.name,m.role,o.billing_owner_id,o.account_type FROM organizations o "
                        "JOIN organization_members m ON m.organization_id=o.id "
                        "WHERE m.user_id=? AND m.status='active' AND m.role IN "
                        "('owner','admin','billing','company_owner','company_admin','member','billing_admin') "
                        "ORDER BY m.created_at,m.organization_id LIMIT 1",
                        (user_id,),
                    ).fetchone()
                    if row:
                        db.execute(
                            "INSERT INTO active_company_selections(user_id,organization_id,updated_at) "
                            "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                            "organization_id=excluded.organization_id,updated_at=excluded.updated_at",
                            (user_id, row[0], _now()),
                        )
                    else:
                        db.execute(
                            "DELETE FROM active_company_selections WHERE user_id=?",
                            (user_id,),
                        )
            else:
                row = db.execute(
                    "SELECT o.id,o.name,m.role,o.billing_owner_id,o.account_type FROM organizations o "
                    "JOIN organization_members m ON m.organization_id=o.id "
                    "WHERE m.user_id=? AND m.role IN "
                    "('owner','admin','billing','company_owner','company_admin','member','billing_admin')"
                    f"{active_clause} ORDER BY m.created_at,m.organization_id LIMIT 1",
                    (user_id,),
                ).fetchone()
        if not row:
            return None
        role = {"owner": "company_owner", "admin": "company_admin",
                "billing": "billing_admin"}.get(str(row[2]), str(row[2]))
        if role not in COMPANY_ROLES:
            return None
        return {"id": row[0], "name": row[1], "role": role,
                "billing_owner_id": row[3], "account_type": row[4]}

    @staticmethod
    def _onboarding_evidence(
            db: sqlite3.Connection, organization_id: str,
            started_at: str) -> tuple[bool, sqlite3.Row | None]:
        checked_at = _now()
        installation = db.execute(
            "SELECT installation.installed_at FROM installations installation "
            "JOIN api_keys credential ON credential.id=installation.registration_key_id "
            "AND credential.key_hash=installation.registration_key_hash "
            "AND credential.organization_id=installation.organization_id "
            "AND credential.key_type='device' "
            "AND credential.revoked_at='' "
            "AND (credential.expires_at='' OR credential.expires_at>?) "
            "JOIN audit_events activation ON activation.organization_id=installation.organization_id "
            "AND activation.action='device_key.activated' "
            "AND activation.target_type='api_key' AND activation.target_id=credential.id "
            "AND activation.outcome='committed' "
            "WHERE installation.organization_id=? AND installation.revoked_at='' "
            "AND installation.device_auth_receipt_id<>'' "
            "AND lower(installation.client_name)='bvx' "
            "AND installation.bvx_version<>'' AND installation.device_id<>'' "
            "AND installation.installed_at>=? ORDER BY installation.installed_at,"
            "installation.id LIMIT 1",
            (checked_at, organization_id, started_at),
        ).fetchone()
        if not installation:
            return False, None
        evidence = db.execute(
            "SELECT usage.id,usage.ts FROM usage_log usage "
            "JOIN installations installation ON installation.organization_id=usage.organization_id "
            "AND installation.registration_key_hash=usage.key_hash "
            "AND installation.revoked_at='' AND installation.device_auth_receipt_id<>'' "
            "AND lower(installation.client_name)='bvx' "
            "AND installation.bvx_version<>'' AND installation.device_id<>'' "
            "AND installation.installed_at>=? AND usage.ts>=installation.installed_at "
            "JOIN api_keys credential ON credential.id=installation.registration_key_id "
            "AND credential.key_hash=installation.registration_key_hash "
            "AND credential.organization_id=usage.organization_id "
            "AND credential.revoked_at='' "
            "AND (credential.expires_at='' OR credential.expires_at>?) "
            "JOIN audit_events activation ON activation.organization_id=usage.organization_id "
            "AND activation.action='device_key.activated' "
            "AND activation.target_type='api_key' AND activation.target_id=credential.id "
            "AND activation.outcome='committed' "
            "WHERE usage.organization_id=? AND usage.authoritative=1 "
            "AND usage.receipt_source='proxy' AND credential.key_type='device' "
            "ORDER BY usage.ts,usage.id LIMIT 1",
            (started_at, checked_at, organization_id),
        ).fetchone()
        return True, evidence

    def onboarding_status(self, user_id: str, organization_id: str) -> dict[str, Any]:
        """Return content-free onboarding state after exact active-membership proof."""
        with self._conn() as db:
            member_columns = {
                row[1] for row in db.execute("PRAGMA table_info(organization_members)")
            }
            active_clause = " AND status='active'" if "status" in member_columns else ""
            member = db.execute(
                "SELECT role FROM organization_members WHERE organization_id=? "
                f"AND user_id=?{active_clause} LIMIT 1",
                (organization_id, user_id),
            ).fetchone()
            if not member or _canonical_store_company_role(member[0]) not in COMPANY_ROLES:
                raise PermissionError("active company membership required")
            organization = db.execute(
                "SELECT onboarding_started_at,onboarding_completed_at "
                "FROM organizations WHERE id=? LIMIT 1",
                (organization_id,),
            ).fetchone()
            if not organization:
                raise PermissionError("active company membership required")
            cli_connected, evidence = self._onboarding_evidence(
                db, organization_id, str(organization[0] or ""),
            )
        completed_at = str(organization[1] or "")
        return {
            "company_id": organization_id,
            "status": "complete" if completed_at else "pending",
            "cli_connected": bool(cli_connected or completed_at),
            "proxied_request_observed": bool(evidence or completed_at),
            "completed_at": completed_at,
        }

    def complete_onboarding(self, user_id: str, organization_id: str,
                            request_id: str) -> dict[str, Any]:
        """Atomically complete onboarding only from durable CLI/proxy evidence."""
        actor_id, audit_request_id, _ = _audit_identity(
            user_id, request_id, "company_owner",
        )
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            member_columns = {
                row[1] for row in db.execute("PRAGMA table_info(organization_members)")
            }
            active_clause = " AND status='active'" if "status" in member_columns else ""
            member = db.execute(
                "SELECT role FROM organization_members WHERE organization_id=? "
                f"AND user_id=?{active_clause} LIMIT 1",
                (organization_id, user_id),
            ).fetchone()
            role = _canonical_store_company_role(member[0]) if member else ""
            if role != "company_owner":
                raise PermissionError("company owner access required")
            organization = db.execute(
                "SELECT onboarding_started_at,onboarding_completed_at "
                "FROM organizations WHERE id=? LIMIT 1",
                (organization_id,),
            ).fetchone()
            if not organization:
                raise PermissionError("active company membership required")
            completed_at = str(organization[1] or "")
            cli_connected, evidence = self._onboarding_evidence(
                db, organization_id, str(organization[0] or ""),
            )
            if completed_at:
                return {
                    "company_id": organization_id,
                    "status": "complete",
                    "cli_connected": True,
                    "proxied_request_observed": True,
                    "completed_at": completed_at,
                }
            if not evidence:
                return {
                    "company_id": organization_id,
                    "status": "pending",
                    "cli_connected": cli_connected,
                    "proxied_request_observed": False,
                    "completed_at": "",
                }
            completed_at = _now()
            updated = db.execute(
                "UPDATE organizations SET onboarding_completed_at=?,"
                "onboarding_completed_by=?,onboarding_evidence_usage_id=? "
                "WHERE id=? AND onboarding_completed_at=''",
                (completed_at, actor_id, int(evidence[0]), organization_id),
            ).rowcount
            if updated:
                db.execute(
                    "INSERT INTO audit_events(organization_id,actor_user_id,action,"
                    "target_type,target_id,details,occurred_at,request_id,actor_id,"
                    "actor_role,outcome) VALUES(?,?,?,?,?,'{}',?,?,?,?,?)",
                    (organization_id, actor_id, "organization.onboarding.completed",
                     "company", organization_id, completed_at, audit_request_id,
                     actor_id, role, "committed"),
                )
        return {
            "company_id": organization_id,
            "status": "complete",
            "cli_connected": True,
            "proxied_request_observed": True,
            "completed_at": completed_at,
        }

    def ensure_service_account(self, organization_id: str, environment: str,
                               created_by: str = "") -> dict[str, Any]:
        name = f"Company backend ({environment})"
        with self._conn() as db:
            row = db.execute("SELECT id,name,environment FROM service_accounts WHERE organization_id=? AND name=? AND environment=?",
                             (organization_id, name, environment)).fetchone()
            if not row:
                service_id = str(uuid.uuid4())
                db.execute("INSERT INTO service_accounts(id,organization_id,name,environment,created_by,created_at) VALUES(?,?,?,?,?,?)",
                           (service_id, organization_id, name, environment, created_by, _now()))
                row = (service_id, name, environment)
        return {"id": row[0], "name": row[1], "environment": row[2]}

    def upsert_customer(self, organization_id: str, external_id: str,
                        display_name: str = "") -> dict[str, Any]:
        return self.upsert_customers(organization_id, [{
            "external_id": external_id, "display_name": display_name,
        }])[0]

    def upsert_customers(self, organization_id: str,
                         customers: list[dict[str, str]]) -> list[dict[str, Any]]:
        if not customers:
            return []
        now = _now()
        imported: list[dict[str, Any]] = []
        with self._conn() as db:
            for customer in customers:
                external_id = customer["external_id"]
                display_name = customer.get("display_name", "")
                db.execute(
                    "INSERT INTO customers(id,organization_id,external_id,display_name,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(organization_id,external_id) DO UPDATE SET "
                    "display_name=CASE WHEN excluded.display_name<>'' THEN excluded.display_name ELSE customers.display_name END, "
                    "updated_at=CASE WHEN excluded.display_name<>'' AND excluded.display_name<>customers.display_name "
                    "THEN excluded.updated_at ELSE customers.updated_at END",
                    (str(uuid.uuid4()), organization_id, external_id, display_name, "active", now, now),
                )
                row = db.execute(
                    "SELECT id,external_id,display_name,status FROM customers "
                    "WHERE organization_id=? AND external_id=?",
                    (organization_id, external_id),
                ).fetchone()
                imported.append({"id": row[0], "external_id": row[1],
                                 "display_name": row[2], "status": row[3]})
        return imported

    def find_customer(self, organization_id: str, external_id: str) -> dict[str, Any] | None:
        with self._conn() as db:
            row = db.execute("SELECT id,external_id,display_name,status FROM customers WHERE organization_id=? AND external_id=?",
                             (organization_id, external_id)).fetchone()
        return ({"id": row[0], "external_id": row[1], "display_name": row[2], "status": row[3]}
                if row else None)

    def list_customers(self, organization_id: str) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute("SELECT id,external_id,display_name,status,created_at,updated_at FROM customers WHERE organization_id=? ORDER BY created_at DESC",
                              (organization_id,)).fetchall()
        return [dict(row) for row in rows]

    def cache_enabled(self, organization_id: str, customer_id: str = "") -> bool:
        if not organization_id:
            return False
        with self._conn() as db:
            if customer_id:
                row = db.execute("SELECT cache_enabled FROM customers WHERE id=? AND organization_id=?",
                                 (customer_id, organization_id)).fetchone()
                if row and bool(row[0]):
                    return True
            row = db.execute("SELECT cache_enabled FROM organizations WHERE id=?",
                             (organization_id,)).fetchone()
        return bool(row and row[0])

    def set_cache_enabled(self, organization_id: str, enabled: bool,
                          customer_id: str = "") -> None:
        with self._conn() as db:
            if customer_id:
                cur = db.execute(
                    "UPDATE customers SET cache_enabled=?,updated_at=? WHERE id=? AND organization_id=?",
                    (int(enabled), _now(), customer_id, organization_id),
                )
            else:
                cur = db.execute("UPDATE organizations SET cache_enabled=? WHERE id=?",
                                 (int(enabled), organization_id))
            if not cur.rowcount:
                raise ValueError("cache tenant not found")

    def create_device_request(self, device_hash: str, expires_at: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM bvx_device_auth WHERE expires_at<=?", (_now(),))
            db.execute("DELETE FROM bvx_device_consumption_receipts WHERE expires_at<=?",
                       (_now(),))
            db.execute("INSERT INTO bvx_device_auth(device_hash,expires_at) VALUES (?,?)",
                       (device_hash, expires_at))

    def get_device_request(self, device_hash: str) -> dict | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT device_hash,expires_at,owner_id,organization_id,key_hash,"
                "encrypted_key,approved_at "
                "FROM bvx_device_auth WHERE device_hash=? AND quarantined_at=''",
                (device_hash,),
            ).fetchone()
            if row:
                return dict(row)
            receipt = db.execute(
                "SELECT device_hash,expires_at,approver_id AS owner_id,organization_id,key_hash,"
                "encrypted_key,consumed_at "
                "FROM bvx_device_consumption_receipts "
                "WHERE device_hash=? AND quarantined_at='' AND expires_at>?",
                (device_hash, _now()),
            ).fetchone()
        if not receipt:
            return None
        recovered = dict(receipt)
        recovered["approved_at"] = recovered.pop("consumed_at")
        return recovered

    def _resolve_device_approval_organization(
            self, db: sqlite3.Connection, owner_id: str,
            selected_organization_id: str = "") -> dict[str, str]:
        member_columns = {
            row[1] for row in db.execute("PRAGMA table_info(organization_members)")
        }
        active_clause = " AND status='active'" if "status" in member_columns else ""
        rows = db.execute(
            "SELECT organization_id,role FROM organization_members WHERE user_id=?"
            f"{active_clause} ORDER BY created_at,organization_id",
            (owner_id,),
        ).fetchall()
        selected = str(selected_organization_id or "")
        if selected:
            try:
                selected = _required_uuid(selected, "selected_organization_id")
            except ValueError as exc:
                raise ValueError("company_access_denied") from exc
            rows = [row for row in rows if str(row["organization_id"]) == selected]
            if len(rows) != 1:
                raise ValueError("company_access_denied")
        elif len(rows) > 1:
            raise ValueError("company_selection_required")
        elif len(rows) != 1:
            raise ValueError("company_access_denied")

        role = {"owner": "company_owner", "admin": "company_admin",
                "billing": "billing_admin"}.get(
                    str(rows[0]["role"]), str(rows[0]["role"]))
        if role not in COMPANY_ROLES:
            raise ValueError("company_access_denied")
        try:
            organization_id = _required_uuid(
                str(rows[0]["organization_id"]), "organization_id")
        except ValueError as exc:
            raise ValueError("company_access_denied") from exc
        return {"id": organization_id, "role": role}

    def resolve_device_approval_organization(
            self, owner_id: str,
            selected_organization_id: str = "") -> dict[str, str]:
        with self._conn() as db:
            return self._resolve_device_approval_organization(
                db, owner_id, selected_organization_id,
            )

    def approve_device_request(self, device_hash: str, owner_id: str, key_hash: str,
                               encrypted_key: str,
                               organization_id: str = "") -> bool:
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            resolved = self._resolve_device_approval_organization(
                db, owner_id, organization_id,
            )
            cur = db.execute(
                "UPDATE bvx_device_auth SET owner_id=?,organization_id=?,key_hash=?,"
                "encrypted_key=?,approved_at=? WHERE device_hash=? AND approved_at='' "
                "AND quarantined_at='' AND expires_at>?",
                (owner_id, resolved["id"], key_hash, encrypted_key, _now(),
                 device_hash, _now()),
            )
        return bool(cur.rowcount)

    def consume_device_request_idempotent(self, device_hash: str,
                                          expected_key_hash: str,
                                          request_id: str) -> dict[str, Any] | None:
        """Activate a device key once and retain a short, request-bound receipt."""
        device_hash, expected_key_hash, request_id = _device_consume_identity(
            device_hash, expected_key_hash, request_id,
        )
        error = ""
        result: dict[str, Any] | None = None
        now = _now()
        with self._conn() as db:
            # BEGIN IMMEDIATE serializes the receipt check, key activation, audit,
            # and exchange deletion for local multi-thread/process test runners.
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM bvx_device_consumption_receipts WHERE expires_at<=?",
                       (now,))
            receipt = db.execute(
                "SELECT id,device_hash,key_hash,encrypted_key,owner_id,approver_id,organization_id,"
                "consumed_at,request_id,expires_at,quarantined_at "
                "FROM bvx_device_consumption_receipts "
                "WHERE device_hash=?", (device_hash,),
            ).fetchone()
            if receipt:
                if receipt["quarantined_at"]:
                    db.execute(
                        "UPDATE bvx_device_consumption_receipts SET encrypted_key='' "
                        "WHERE device_hash=?", (device_hash,),
                    )
                    _append_sqlite_device_denial_audit(
                        db, organization_id=receipt["organization_id"],
                        request_id=request_id, target_type="device_receipt",
                        target_id=receipt["id"],
                    )
                    error = "device consumption receipt quarantined"
                elif not hmac.compare_digest(receipt["key_hash"], expected_key_hash):
                    existing_key = db.execute(
                        "SELECT id FROM api_keys WHERE key_hash=?",
                        (receipt["key_hash"],),
                    ).fetchone()
                    db.execute(
                        "UPDATE api_keys SET revoked_at=? WHERE key_hash=? "
                        "AND organization_id=? AND revoked_at=''",
                        (now, receipt["key_hash"], receipt["organization_id"]),
                    )
                    db.execute(
                        "UPDATE bvx_device_consumption_receipts SET encrypted_key='',"
                        "quarantined_at=? WHERE device_hash=?", (now, device_hash),
                    )
                    _append_sqlite_device_denial_audit(
                        db, organization_id=receipt["organization_id"],
                        request_id=request_id,
                        target_type="api_key" if existing_key else "device_receipt",
                        target_id=(str(existing_key["id"]) if existing_key
                                   else receipt["id"]),
                    )
                    error = "device credential digest mismatch quarantined"
                else:
                    key = db.execute(
                        "SELECT id,key_hash,owner_id,organization_id,key_type,"
                        "revoked_at,expires_at FROM api_keys WHERE key_hash=?",
                        (receipt["key_hash"],),
                    ).fetchone()
                    member_columns = {
                        row[1] for row in db.execute(
                            "PRAGMA table_info(organization_members)")
                    }
                    active_clause = (
                        " AND status='active'" if "status" in member_columns else ""
                    )
                    member = db.execute(
                        "SELECT role FROM organization_members WHERE organization_id=? "
                        f"AND user_id=?{active_clause} LIMIT 1",
                        (receipt["organization_id"], receipt["owner_id"]),
                    ).fetchone()
                    member_role = ({"owner": "company_owner",
                                    "admin": "company_admin",
                                    "billing": "billing_admin"}.get(
                                        str(member["role"]), str(member["role"])
                                    ) if member else "")
                    approver = db.execute(
                        "SELECT role FROM organization_members WHERE organization_id=? "
                        f"AND user_id=?{active_clause} LIMIT 1",
                        (receipt["organization_id"], receipt["approver_id"]),
                    ).fetchone()
                    approver_role = ({"owner": "company_owner",
                                      "admin": "company_admin",
                                      "billing": "billing_admin"}.get(
                                          str(approver["role"]),
                                          str(approver["role"])
                                      ) if approver else "")
                    activation_audit = (db.execute(
                        "SELECT 1 FROM audit_events WHERE organization_id=? "
                        "AND actor_id=? AND actor_role IN "
                        "('company_owner','company_admin','member','billing_admin') "
                        "AND request_id=? AND action='device_key.activated' "
                        "AND target_type='api_key' AND target_id=? "
                        "AND outcome='committed' AND details='{}' LIMIT 1",
                        (receipt["organization_id"], receipt["approver_id"],
                         receipt["request_id"], str(key["id"]) if key else ""),
                    ).fetchone() if receipt["approver_id"] else None)
                    key_valid = bool(
                        key
                        and hmac.compare_digest(key["key_hash"], receipt["key_hash"])
                        and key["organization_id"] == receipt["organization_id"]
                        and key["owner_id"] == receipt["owner_id"]
                        and key["key_type"] == "device"
                        and not key["revoked_at"]
                        and _credential_unexpired(key["expires_at"], now)
                    )
                    if (not key_valid or not member
                            or member_role not in COMPANY_ROLES
                            or not approver or approver_role not in COMPANY_ROLES
                            or not activation_audit):
                        target_id = (str(key["id"]) if key and key["id"]
                                     else str(uuid.uuid4()))
                        db.execute(
                            "UPDATE bvx_device_consumption_receipts SET encrypted_key='',"
                            "quarantined_at=? WHERE device_hash=?", (now, device_hash),
                        )
                        _append_sqlite_device_denial_audit(
                            db, organization_id=receipt["organization_id"],
                            request_id=request_id,
                            target_type="api_key" if key else "device_receipt",
                            target_id=target_id if key else receipt["id"],
                        )
                        error = "device consumption receipt validation failed"
                    else:
                        # The stored request ID remains the immutable activation/audit
                        # identity. A client retry can carry a new middleware ID, but
                        # only while the exact activated key and membership stay valid.
                        result = {
                            "status": "consumed", "already_consumed": True,
                            "device_hash": receipt["device_hash"],
                            "key_hash": receipt["key_hash"],
                            "encrypted_key": receipt["encrypted_key"],
                            "owner_id": receipt["owner_id"],
                            "organization_id": receipt["organization_id"],
                            "consumed_at": receipt["consumed_at"],
                        }
            else:
                exchange = db.execute(
                    "SELECT device_hash,expires_at,owner_id,organization_id,key_hash,"
                    "encrypted_key FROM bvx_device_auth WHERE device_hash=? "
                    "AND approved_at<>'' AND quarantined_at='' AND expires_at>?",
                    (device_hash, now),
                ).fetchone()
                if exchange:
                    organization_id = str(exchange["organization_id"] or "")
                    member_columns = {
                        row[1] for row in db.execute(
                            "PRAGMA table_info(organization_members)")
                    }
                    active_clause = (
                        " AND status='active'" if "status" in member_columns else ""
                    )
                    member = db.execute(
                        "SELECT organization_id,role FROM organization_members "
                        "WHERE user_id=? AND organization_id=? "
                        f"{active_clause} ORDER BY created_at LIMIT 1",
                        (exchange["owner_id"], organization_id),
                    ).fetchone()
                    role = ({"owner": "company_owner", "admin": "company_admin",
                             "billing": "billing_admin"}.get(
                                 str(member["role"]), str(member["role"])
                             ) if member else "")
                    if (not member or not organization_id
                            or role not in COMPANY_ROLES):
                        denial_target_id = str(uuid.uuid4())
                        db.execute(
                            "UPDATE bvx_device_auth SET quarantined_at=?,encrypted_key='' "
                            "WHERE device_hash=?", (now, device_hash),
                        )
                        _append_sqlite_device_denial_audit(
                            db, organization_id=organization_id,
                            request_id=request_id, target_type="device_receipt",
                            target_id=denial_target_id,
                        )
                        error = "device consume tenant binding unavailable"
                    elif not hmac.compare_digest(
                            exchange["key_hash"], expected_key_hash):
                        existing_key = db.execute(
                            "SELECT id FROM api_keys WHERE key_hash=?",
                            (exchange["key_hash"],),
                        ).fetchone()
                        denial_target_id = (str(existing_key["id"]) if existing_key
                                            else str(uuid.uuid4()))
                        db.execute(
                            "UPDATE bvx_device_auth SET quarantined_at=?,encrypted_key='' "
                            "WHERE device_hash=?", (now, device_hash),
                        )
                        db.execute(
                            "UPDATE api_keys SET revoked_at=? WHERE key_hash=? "
                            "AND organization_id=? AND revoked_at=''",
                            (now, exchange["key_hash"], organization_id),
                        )
                        _append_sqlite_device_denial_audit(
                            db, organization_id=organization_id,
                            request_id=request_id,
                            target_type="api_key" if existing_key else "device_receipt",
                            target_id=denial_target_id,
                        )
                        error = "device credential digest mismatch quarantined"
                    else:
                        existing = db.execute(
                            "SELECT id FROM api_keys WHERE key_hash=?",
                            (exchange["key_hash"],),
                        ).fetchone()
                        if existing:
                            db.execute(
                                "UPDATE bvx_device_auth SET quarantined_at=?,encrypted_key='' "
                                "WHERE device_hash=?", (now, device_hash),
                            )
                            db.execute(
                                "UPDATE api_keys SET revoked_at=? WHERE key_hash=?",
                                (now, exchange["key_hash"]),
                            )
                            _append_sqlite_device_denial_audit(
                                db, organization_id=organization_id,
                                request_id=request_id, target_type="api_key",
                                target_id=str(existing["id"]),
                            )
                            error = "device key activation conflict quarantined"
                        else:
                            billing_owner = db.execute(
                                "SELECT billing_owner_id FROM organizations WHERE id=?",
                                (organization_id,),
                            ).fetchone()
                            key_id = str(uuid.uuid4())
                            owner_id = exchange["owner_id"]
                            if billing_owner and billing_owner[0]:
                                billing_member = db.execute(
                                    "SELECT role FROM organization_members "
                                    "WHERE organization_id=? AND user_id=?"
                                    f"{active_clause} LIMIT 1",
                                    (organization_id, str(billing_owner[0])),
                                ).fetchone()
                                billing_role = ({"owner": "company_owner",
                                                 "admin": "company_admin",
                                                 "billing": "billing_admin"}.get(
                                                     str(billing_member["role"]),
                                                     str(billing_member["role"])
                                                 ) if billing_member else "")
                                if billing_role in COMPANY_ROLES:
                                    owner_id = str(billing_owner[0])
                            db.execute(
                                "INSERT INTO api_keys(id,key_hash,name,created,owner_id,"
                                "organization_id,key_type,scopes) VALUES (?,?,?,?,?,?,?,?)",
                                (key_id, exchange["key_hash"], "bvx device", now,
                                 owner_id, organization_id, "device",
                                 "proxy:invoke,usage:write,repositories:register,"
                                 "installations:register,customers:import"),
                            )
                            db.execute(
                                "INSERT INTO bvx_device_consumption_receipts("
                                "device_hash,id,key_hash,encrypted_key,owner_id,"
                                "approver_id,organization_id,consumed_at,expires_at,request_id) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (device_hash, str(uuid.uuid4()), exchange["key_hash"],
                                 exchange["encrypted_key"], owner_id, exchange["owner_id"],
                                 organization_id, now, exchange["expires_at"], request_id),
                            )
                            db.execute(
                                "INSERT INTO audit_events(organization_id,actor_user_id,"
                                "action,target_type,target_id,details,occurred_at,request_id,"
                                "actor_id,actor_role,outcome) VALUES(?,?,?,?,?,'{}',?,?,?,?,?)",
                                (organization_id, exchange["owner_id"],
                                 "device_key.activated", "api_key", key_id, now,
                                 request_id, exchange["owner_id"], role, "committed"),
                            )
                            db.execute("DELETE FROM bvx_device_auth WHERE device_hash=?",
                                       (device_hash,))
                            result = {
                                "status": "consumed", "already_consumed": False,
                                "device_hash": device_hash,
                                "key_hash": exchange["key_hash"],
                                "encrypted_key": exchange["encrypted_key"],
                                "owner_id": owner_id,
                                "organization_id": organization_id,
                                "consumed_at": now,
                            }
        if error:
            raise RuntimeError(error)
        if result is None:
            return None
        return _validated_device_receipt(
            result, device_hash=device_hash, expected_key_hash=expected_key_hash,
        )

    def consume_device_request(self, device_hash: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT owner_id,key_hash,encrypted_key FROM bvx_device_auth WHERE device_hash=? AND approved_at<>'' AND expires_at>?",
                             (device_hash, _now())).fetchone()
            if not row:
                return None
            member = db.execute("SELECT organization_id FROM organization_members WHERE user_id=? ORDER BY created_at LIMIT 1",
                                (row["owner_id"],)).fetchone()
            organization_id = member[0] if member else ""
            billing_owner = db.execute("SELECT billing_owner_id FROM organizations WHERE id=?",
                                       (organization_id,)).fetchone()
            db.execute("INSERT INTO api_keys(id,key_hash,name,created,owner_id,organization_id,key_type,scopes) VALUES (?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), row["key_hash"], "bvx device", _now(),
                        billing_owner[0] if billing_owner and billing_owner[0] else row["owner_id"],
                        organization_id, "device",
                        "proxy:invoke,usage:write,repositories:register,installations:register,customers:import"))
            db.execute("DELETE FROM bvx_device_auth WHERE device_hash=?", (device_hash,))
        return dict(row)

    def create_key(self, key_hash: str, name: str, owner_id: str = "", *,
                   organization_id: str = "", service_account_id: str = "",
                   key_type: str = "legacy", scopes: list[str] | None = None,
                   environment: str = "", key_prefix: str = "",
                   created_by: str = "", expires_at: str = "",
                   request_id: str = "", actor_role: str = "legacy") -> None:
        scope_value = ",".join(scopes or ["proxy:invoke", "usage:write", "usage:read_own",
                                          "repositories:register"])
        key_id = str(uuid.uuid4())
        audit_identity = (_audit_identity(created_by, request_id, actor_role)
                          if organization_id else None)
        if key_type == "dashboard_session":
            expires_at = _dashboard_expiry(expires_at)
        try:
            with self._conn() as db:
                if key_type == "dashboard_session":
                    # Serialize expiry cleanup, cap enforcement, rotation, and
                    # insertion across local API processes sharing this file.
                    db.execute("BEGIN IMMEDIATE")
                    now = _now()
                    db.execute(
                        "UPDATE api_keys SET revoked_at=? WHERE organization_id=? "
                        "AND key_type='dashboard_session' AND revoked_at='' "
                        "AND (expires_at='' OR expires_at<=?)",
                        (now, organization_id, now),
                    )
                    counts = db.execute(
                        "SELECT count(*),sum(CASE WHEN created_by=? THEN 1 ELSE 0 END) "
                        "FROM api_keys WHERE organization_id=? "
                        "AND key_type='dashboard_session' AND revoked_at='' "
                        "AND expires_at>?",
                        (created_by, organization_id, now),
                    ).fetchone()
                    company_active = int(counts[0] or 0)
                    actor_active = int(counts[1] or 0)
                    revoke_count = max(
                        actor_active - (DASHBOARD_SESSION_PER_ACTOR_CAP - 1),
                        company_active - (DASHBOARD_SESSION_PER_COMPANY_CAP - 1),
                        0,
                    )
                    if revoke_count > actor_active:
                        raise RuntimeError("dashboard session company cap reached")
                    rotated = db.execute(
                        "SELECT id FROM api_keys WHERE organization_id=? "
                        "AND key_type='dashboard_session' AND created_by=? "
                        "AND revoked_at='' AND expires_at>? ORDER BY created,id LIMIT ?",
                        (organization_id, created_by, now, revoke_count),
                    ).fetchall()
                    actor_id, audit_request_id, audit_role = audit_identity
                    for row in rotated:
                        db.execute(
                            "UPDATE api_keys SET revoked_at=? WHERE organization_id=? "
                            "AND id=? AND key_type='dashboard_session' AND created_by=? "
                            "AND revoked_at=''",
                            (now, organization_id, row["id"], created_by),
                        )
                        db.execute(
                            "INSERT INTO audit_events(organization_id,actor_user_id,"
                            "action,target_type,target_id,details,occurred_at,request_id,"
                            "actor_id,actor_role,outcome) "
                            "VALUES(?,?,?,?,?,'{}',?,?,?,?,?)",
                            (organization_id, created_by, "dashboard_session.rotated",
                             "api_key", row["id"], now, audit_request_id,
                             actor_id, audit_role, "committed"),
                        )
                if key_type == "organization_service":
                    billing_owner = db.execute(
                        "SELECT organization.billing_owner_id FROM organizations organization "
                        "JOIN service_accounts account "
                        "ON account.organization_id=organization.id "
                        "WHERE organization.id=? AND account.id=?",
                        (organization_id, service_account_id),
                    ).fetchone()
                    if not billing_owner or not billing_owner[0]:
                        raise ValueError(
                            "organization service key requires an authoritative billing owner")
                    owner_id = str(billing_owner[0])
                insert = ("INSERT" if key_type == "dashboard_session"
                          else "INSERT OR IGNORE")
                cur = db.execute(f"{insert} INTO api_keys(id,key_hash,name,created,owner_id,organization_id,service_account_id,key_type,scopes,environment,key_prefix,created_by,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (key_id, key_hash, name, _now(), owner_id, organization_id,
                            service_account_id, key_type, scope_value, environment,
                            key_prefix, created_by, expires_at))
                if cur.rowcount and organization_id:
                    actor_id, audit_request_id, audit_role = audit_identity
                    db.execute(
                        "INSERT INTO audit_events(organization_id,actor_user_id,action,"
                        "target_type,target_id,details,occurred_at,request_id,actor_id,"
                        "actor_role,outcome) VALUES(?,?,?,?,?,'{}',?,?,?,?,?)",
                        (organization_id, created_by, "api_key.created", "api_key",
                         key_id, _now(), audit_request_id, actor_id, audit_role, "committed"),
                    )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("duplicate key") from exc

    def key_exists(self, key_hash: str) -> bool:
        with self._conn() as db:
            return db.execute("SELECT 1 FROM api_keys WHERE key_hash=? AND revoked_at='' AND (expires_at='' OR expires_at>?)",
                              (key_hash, _now())).fetchone() is not None

    def key_context(self, key_hash: str) -> dict[str, Any] | None:
        with self._conn() as db:
            row = db.execute("SELECT key_hash,owner_id,organization_id,service_account_id,key_type,scopes,environment FROM api_keys WHERE key_hash=? AND revoked_at='' AND (expires_at='' OR expires_at>?)",
                             (key_hash, _now())).fetchone()
            if row:
                db.execute("UPDATE api_keys SET last_used_at=? WHERE key_hash=?", (_now(), key_hash))
        if not row:
            return None
        result = dict(row)
        result["scopes"] = [scope for scope in str(result.get("scopes") or "").split(",") if scope]
        return result

    def key_owner(self, key_hash: str) -> str:
        with self._conn() as db:
            row = db.execute(
                "SELECT CASE WHEN key.key_type='organization_service' "
                "THEN organization.billing_owner_id ELSE key.owner_id END "
                "FROM api_keys key LEFT JOIN organizations organization "
                "ON organization.id=key.organization_id WHERE key.key_hash=?",
                (key_hash,),
            ).fetchone()
        return str(row[0] or "") if row else ""

    def list_keys(self, key_hash: str = "") -> list[dict[str, Any]]:
        owner = self.key_owner(key_hash) if key_hash else ""
        with self._conn() as db:
            if owner:
                rows = db.execute("SELECT key_hash,name,created FROM api_keys WHERE owner_id=? ORDER BY created DESC", (owner,)).fetchall()
            elif key_hash:
                rows = db.execute("SELECT key_hash,name,created FROM api_keys WHERE key_hash=?", (key_hash,)).fetchall()
            else:
                rows = db.execute("SELECT key_hash,name,created FROM api_keys ORDER BY created DESC").fetchall()
        return [{"id": r[0], "name": r[1], "created": r[2]} for r in rows]

    def list_organization_keys(self, organization_id: str) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute("SELECT id,key_hash,name,created,key_type,scopes,environment,key_prefix,last_used_at,revoked_at FROM api_keys WHERE organization_id=? ORDER BY created DESC",
                              (organization_id,)).fetchall()
        return [{"id": row[0], "fingerprint": row[1][:16], "name": row[2], "created": row[3],
                 "key_type": row[4],
                 "scopes": [scope for scope in str(row[5] or "").split(",") if scope],
                 "environment": row[6], "prefix": row[7], "last_used_at": row[8],
                 "revoked_at": row[9]} for row in rows]

    def revoke_organization_key(self, organization_id: str, target_key_id: str,
                                actor_user_id: str = "", request_id: str = "",
                                actor_role: str = "legacy") -> bool:
        actor_id, audit_request_id, audit_role = _audit_identity(
            actor_user_id, request_id, actor_role,
        )
        with self._conn() as db:
            cur = db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND organization_id=? AND revoked_at=''",
                             (_now(), target_key_id, organization_id))
            if cur.rowcount:
                db.execute(
                    "INSERT INTO audit_events(organization_id,actor_user_id,action,"
                    "target_type,target_id,details,occurred_at,request_id,actor_id,"
                    "actor_role,outcome) VALUES(?,?,?,?,?,'{}',?,?,?,?,?)",
                    (organization_id, actor_user_id, "api_key.revoked", "api_key",
                     target_key_id, _now(), audit_request_id, actor_id, audit_role,
                     "committed"),
                )
        return bool(cur.rowcount)

    def revoke_keys_by_type(self, organization_id: str, key_type: str,
                            actor_user_id: str = "") -> int:
        with self._conn() as db:
            cur = db.execute(
                "UPDATE api_keys SET revoked_at=? WHERE organization_id=? AND key_type=? "
                "AND created_by=? AND revoked_at=''",
                (_now(), organization_id, key_type, actor_user_id),
            )
        return int(cur.rowcount or 0)

    def register_installation(self, organization_id: str, service_account_id: str,
                              installation_id: str, repository: str | None, environment: str,
                              bvx_version: str, device_fingerprint: str = "", *,
                              repository_id: str = "", device_platform: str = "",
                              device_arch: str = "", client_name: str = "",
                              registration_key_hash: str = "") -> dict[str, Any]:
        now = _now()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            registration_key_id = ""
            device_auth_receipt_id = ""
            if registration_key_hash:
                if not _SHA256_DIGEST.fullmatch(registration_key_hash):
                    raise ValueError("invalid installation credential")
                credential = db.execute(
                    "SELECT id,key_type,scopes FROM api_keys WHERE key_hash=? "
                    "AND organization_id=? AND revoked_at='' "
                    "AND (expires_at='' OR expires_at>?) LIMIT 1",
                    (registration_key_hash, organization_id, now),
                ).fetchone()
                if not credential:
                    raise ValueError("installation credential is not active")
                if "installations:register" not in {
                        scope for scope in str(credential[2] or "").split(",") if scope}:
                    raise ValueError("installation credential lacks registration scope")
                registration_key_id = str(credential[0])
                if str(credential[1]) == "device":
                    receipt = db.execute(
                        "SELECT id FROM bvx_device_consumption_receipts "
                        "WHERE key_hash=? AND organization_id=? AND quarantined_at='' "
                        "ORDER BY consumed_at DESC LIMIT 1",
                        (registration_key_hash, organization_id),
                    ).fetchone()
                    activation = db.execute(
                        "SELECT 1 FROM audit_events WHERE organization_id=? "
                        "AND action='device_key.activated' AND target_type='api_key' "
                        "AND target_id=? AND outcome='committed' LIMIT 1",
                        (organization_id, registration_key_id),
                    ).fetchone()
                    if receipt and activation:
                        device_auth_receipt_id = str(receipt[0])
            device_id = ""
            if device_fingerprint:
                device = db.execute("SELECT id,revoked_at FROM devices WHERE organization_id=? AND device_fingerprint=?",
                                    (organization_id, device_fingerprint)).fetchone()
                if device:
                    if device[1]:
                        raise ValueError("device is revoked")
                    device_id = device[0]
                    db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now, device_id))
                else:
                    device_id = str(uuid.uuid4())
                    db.execute("INSERT INTO devices(id,organization_id,device_fingerprint,created_at,last_seen_at) VALUES(?,?,?,?,?)",
                               (device_id, organization_id, device_fingerprint, now, now))
            row = db.execute(
                "SELECT id,revoked_at,registration_key_hash,registration_key_id,"
                "device_auth_receipt_id FROM installations "
                "WHERE id=? AND organization_id=?",
                             (installation_id, organization_id)).fetchone()
            if row:
                if row[1]:
                    raise ValueError("installation is revoked")
                if row[2] and row[2] != registration_key_hash:
                    raise ValueError("installation credential mismatch")
                if row[3] and row[3] != registration_key_id:
                    raise ValueError("installation credential mismatch")
                if row[4] and row[4] != device_auth_receipt_id:
                    raise ValueError("installation device authorization mismatch")
                registration_key_hash = str(row[2] or registration_key_hash)
                registration_key_id = str(row[3] or registration_key_id)
                device_auth_receipt_id = str(row[4] or device_auth_receipt_id)
                if repository is None:
                    current = db.execute("SELECT repository_id,repository FROM installations WHERE id=?",
                                         (installation_id,)).fetchone()
                    repository_id, repository = current[0], current[1]
                db.execute("UPDATE installations SET device_id=?,repository_id=?,repository=?,environment=?,device_platform=?,device_arch=?,client_name=?,bvx_version=?,last_seen_at=?,registration_key_hash=?,registration_key_id=?,device_auth_receipt_id=? WHERE id=?",
                           (device_id, repository_id, repository or "", environment, device_platform,
                            device_arch, client_name, bvx_version, now, registration_key_hash,
                            registration_key_id, device_auth_receipt_id, installation_id))
            else:
                db.execute("INSERT INTO installations(id,organization_id,device_id,service_account_id,repository_id,repository,environment,device_platform,device_arch,client_name,bvx_version,installed_at,last_seen_at,registration_key_hash,registration_key_id,device_auth_receipt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (installation_id, organization_id, device_id, service_account_id,
                            repository_id, repository or "", environment, device_platform, device_arch,
                            client_name, bvx_version, now, now, registration_key_hash,
                            registration_key_id, device_auth_receipt_id))
        return {"id": installation_id, "last_seen_at": now}

    def list_installations(self, organization_id: str) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute("SELECT id,repository_id,repository,environment,device_platform,device_arch,client_name,bvx_version,installed_at,last_seen_at,revoked_at FROM installations WHERE organization_id=? ORDER BY last_seen_at DESC",
                              (organization_id,)).fetchall()
        return [dict(row) for row in rows]

    def organization_inventory(self, organization_id: str) -> dict[str, Any]:
        with self._conn() as db:
            members = [dict(row) for row in db.execute(
                "SELECT user_id,role,created_at FROM organization_members WHERE organization_id=? ORDER BY created_at",
                (organization_id,),
            ).fetchall()]
            devices = [dict(row) for row in db.execute(
                "SELECT id,display_name,created_at,last_seen_at,revoked_at FROM devices WHERE organization_id=? ORDER BY last_seen_at DESC",
                (organization_id,),
            ).fetchall()]
        customers = self.list_customers(organization_id)
        keys = self.list_organization_keys(organization_id)
        installations = self.list_installations(organization_id)
        return {
            "counts": {"members": len(members), "customers": len(customers),
                       "keys": len(keys), "devices": len(devices),
                       "installations": len(installations)},
            "members": members, "customers": customers, "keys": keys,
            "devices": devices, "installations": installations,
        }

    def revoke_installation(self, organization_id: str, installation_id: str) -> bool:
        with self._conn() as db:
            cur = db.execute(
                "UPDATE installations SET revoked_at=? WHERE id=? AND organization_id=? AND revoked_at=''",
                (_now(), installation_id, organization_id),
            )
        return bool(cur.rowcount)

    def register_repository(self, key_hash: str, repo: str, source: str = "bvx") -> None:
        owner = self.key_owner(key_hash)
        now = _now()
        with self._conn() as db:
            db.execute(
                "INSERT INTO key_repositories(key_hash,owner_id,repo,source,installed_at,last_seen) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(key_hash,repo) DO UPDATE SET "
                "owner_id=excluded.owner_id,source=excluded.source,last_seen=excluded.last_seen",
                (key_hash, owner, repo, source, now, now),
            )

    def get_admin_key_inventory(self) -> dict[str, Any]:
        with self._conn() as db:
            keys = [dict(row) for row in db.execute(
                "SELECT key_hash,name,created,owner_id FROM api_keys ORDER BY created DESC LIMIT ?",
                (ADMIN_RESULT_MAX,))]
            repositories = [dict(row) for row in db.execute(
                "SELECT key_hash,owner_id,repo,source,installed_at,last_seen "
                "FROM key_repositories ORDER BY last_seen DESC LIMIT ?",
                (ADMIN_RESULT_MAX * 4,))]
            usage = [dict(row) for row in db.execute(
                "SELECT key_hash,owner_id,repo,project,max(ts) AS ts FROM usage_log "
                "GROUP BY key_hash,owner_id,repo,project ORDER BY ts DESC LIMIT ?",
                (ADMIN_RESULT_MAX * 4,))]
        result = _admin_key_inventory(keys, repositories, usage)
        result["truncated"] = len(keys) == ADMIN_RESULT_MAX
        return result

    def delete_key(self, current_key_hash: str, target_key_hash: str) -> bool:
        owner = self.key_owner(current_key_hash)
        with self._conn() as db:
            if owner:
                cur = db.execute("DELETE FROM api_keys WHERE key_hash=? AND owner_id=?",
                                 (target_key_hash, owner))
            else:
                cur = db.execute("DELETE FROM api_keys WHERE key_hash=? AND key_hash=?",
                                 (target_key_hash, current_key_hash))
        return bool(cur.rowcount)

    def has_request(self, key_hash: str, request_id: str) -> bool:
        if not request_id:
            return False
        with self._conn() as db:
            return db.execute("SELECT 1 FROM usage_log WHERE key_hash=? AND request_id=?", (key_hash, request_id)).fetchone() is not None

    def record_usage(self, key_hash: str, baseline_tokens: int, optimized_tokens: int,
                     savings_pct: float = 0, quality_proxy: Optional[float] = None,
                     **values: Any) -> bool:
        row = _usage_row(key_hash, baseline_tokens, optimized_tokens, savings_pct, quality_proxy, **values)
        columns = list(row)
        try:
            with self._conn() as db:
                cur = db.execute(f"INSERT INTO usage_log ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                                 [row[c] for c in columns])
            return bool(cur.rowcount)
        except sqlite3.IntegrityError:
            return False

    def record_usage_batch(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Commit one bounded batch; valid rows survive malformed/duplicate siblings."""
        if len(records) > USAGE_BATCH_MAX:
            raise ValueError(f"usage batch exceeds maximum of {USAGE_BATCH_MAX}")
        result = {"read": len(records), "inserted": 0, "duplicates": 0, "failed": 0}
        failed_records: list[dict[str, Any]] = []
        with self._conn() as db:
            for record in records:
                try:
                    row = _batch_record_row(record)
                    columns = list(row)
                    db.execute(
                        f"INSERT INTO usage_log ({','.join(columns)}) VALUES "
                        f"({','.join('?' for _ in columns)})",
                        [row[column] for column in columns],
                    )
                    result["inserted"] += 1
                except (TypeError, ValueError):
                    result["failed"] += 1
                    failed_records.append(dict(record))
                except sqlite3.IntegrityError:
                    request_id = str(record.get("request_id") or "")
                    key_hash = str(record.get("key_hash") or "")
                    duplicate = bool(request_id and db.execute(
                        "SELECT 1 FROM usage_log WHERE key_hash=? AND request_id=?",
                        (key_hash, request_id),
                    ).fetchone())
                    result["duplicates" if duplicate else "failed"] += 1
                    if not duplicate:
                        failed_records.append(dict(record))
        if failed_records:
            result["failed_records"] = failed_records
        return result

    def list_usage_page(self, key_hash: str, *, cursor: str = "",
                        limit: int = USAGE_PAGE_DEFAULT) -> dict[str, Any]:
        """Return a stable `(ts,id)` keyset page scoped to the caller's tenant."""
        page_limit = _bounded_limit(limit)
        decoded = _decode_usage_cursor(cursor)
        context = self.key_context(key_hash)
        organization_id = str((context or {}).get("organization_id") or "")
        owner = self.key_owner(key_hash)
        predicates: list[str] = []
        values: list[Any] = []
        if organization_id:
            predicates.append("organization_id=?")
            values.append(organization_id)
        elif owner:
            predicates.append("(owner_id=? OR key_hash=?)")
            values.extend((owner, key_hash))
        else:
            predicates.append("key_hash=?")
            values.append(key_hash)
        if decoded:
            predicates.append("(ts < ? OR (ts = ? AND id < ?))")
            values.extend((decoded[0], decoded[0], decoded[1]))
        values.append(page_limit + 1)
        with self._conn() as db:
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM usage_log WHERE {' AND '.join(predicates)} "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                values,
            ).fetchall()]
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        next_cursor = (_encode_usage_cursor(rows[-1]["ts"], rows[-1]["id"])
                       if has_more and rows else "")
        return {"rows": rows, "next_cursor": next_cursor, "limit": page_limit}

    def _rows(self, key_hash: str) -> list[dict[str, Any]]:
        context = self.key_context(key_hash)
        organization_id = str((context or {}).get("organization_id") or "")
        owner = self.key_owner(key_hash)
        with self._conn() as db:
            if organization_id:
                return [dict(r) for r in db.execute(
                    "SELECT * FROM usage_log WHERE organization_id=? ORDER BY ts DESC,id DESC LIMIT ?",
                    (organization_id, LOCAL_USAGE_SCAN_MAX))]
            if owner:
                query = "SELECT * FROM usage_log WHERE owner_id=? OR key_hash=? ORDER BY ts DESC,id DESC LIMIT ?"
                return [dict(r) for r in db.execute(query, (owner, key_hash, LOCAL_USAGE_SCAN_MAX))]
            return [dict(r) for r in db.execute(
                "SELECT * FROM usage_log WHERE key_hash=? ORDER BY ts DESC,id DESC LIMIT ?",
                (key_hash, LOCAL_USAGE_SCAN_MAX))]

    def _all_rows(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM usage_log ORDER BY ts DESC,id DESC LIMIT ?",
                (LOCAL_USAGE_SCAN_MAX,))]

    def get_stats(self, key_hash: str) -> dict[str, Any]:
        rows = self._rows(key_hash)
        result = _stats(rows)
        result["by_pipeline"] = self.get_stats_by_pipeline(key_hash)
        result["by_agent"] = self.get_stats_by_agent(key_hash)
        return result

    def get_breakdown(self, key_hash: str) -> list[dict[str, Any]]:
        return _breakdown(self._rows(key_hash))

    def get_admin_stats(self) -> dict[str, Any]:
        return _stats(self._all_rows())

    def get_admin_breakdown(self) -> list[dict[str, Any]]:
        return _admin_breakdown(self._all_rows())

    def get_admin_report(self, filters: dict[str, str]) -> dict[str, Any]:
        rows = _filter_admin_rows(self._all_rows(), filters)
        return {"totals": _stats(rows), "rows": _admin_breakdown(rows)}

    def get_admin_report_page(self, filters: dict[str, str], *,
                              sort: str = "actual_cost_usd", direction: str = "desc",
                              cursor: str = "", limit: int = USAGE_PAGE_DEFAULT) -> dict[str, Any]:
        if sort not in ADMIN_SORT_FIELDS or direction not in ("asc", "desc"):
            raise ValueError("unsupported admin report ordering")
        page_limit = _bounded_limit(limit, ADMIN_RESULT_MAX)
        decoded = _decode_admin_cursor(cursor, sort, direction)
        report = self.get_admin_report(filters)
        rows = report["rows"]
        for row in rows:
            row["_sort_value"] = float(row.get(sort) or 0)
            row["_row_key"] = _admin_row_key(row)
        rows.sort(key=lambda row: (row["_sort_value"], row["_row_key"]),
                  reverse=direction == "desc")
        if decoded:
            cursor_key = (float(decoded[0]), decoded[1])
            rows = [row for row in rows if (
                (row["_sort_value"], row["_row_key"]) > cursor_key
                if direction == "asc"
                else (row["_sort_value"], row["_row_key"]) < cursor_key
            )]
        page = rows[:page_limit + 1]
        has_more = len(page) > page_limit
        page = page[:page_limit]
        next_cursor = (_encode_admin_cursor(
            sort, direction, page[-1]["_sort_value"], page[-1]["_row_key"]
        ) if has_more and page else "")
        for row in page:
            row.pop("_sort_value", None)
            row.pop("_row_key", None)
        return {"rows": page, "totals": report["totals"],
                "pagination": {"total": len(report["rows"]), "limit": page_limit,
                               "next_cursor": next_cursor, "has_more": has_more},
                "sort": sort, "direction": direction}

    def _legacy_group(self, key_hash: str, field: str, pipeline: str = "") -> list[dict[str, Any]]:
        rows = self._rows(key_hash)
        if pipeline:
            rows = [r for r in rows if r.get("pipeline") == pipeline]
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(row.get(field) or "")].append(row)
        out = []
        for label, items in groups.items():
            stat = _stats(items)
            out.append({field: label, "calls": len(items), "tokens_saved": stat["total_tokens_saved"],
                        "provider_input_tokens_avoided": stat[
                            "total_provider_input_tokens_avoided"],
                        "calls_avoided": stat["total_calls_avoided"],
                        "native_cache_discount_usd": stat[
                            "total_native_cache_discount_usd"],
                        "transport_bytes_avoided": stat[
                            "total_transport_bytes_avoided"],
                        "avg_savings_pct": stat["avg_savings_pct"], "avg_quality": stat["avg_quality_proxy"],
                        "cost_saved_usd": stat["total_verified_savings_usd"],
                        "brevitas_fee_usd": stat["total_brevitas_fee_usd"]})
        return sorted(out, key=lambda r: -r["tokens_saved"])

    def get_stats_by_pipeline(self, key_hash: str, start: str = "", end: str = "") -> list:
        return self._legacy_group(key_hash, "pipeline")

    def get_stats_by_agent(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "") -> list:
        return self._legacy_group(key_hash, "agent", pipeline)

    def get_stats_by_run(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "") -> list:
        return self._legacy_group(key_hash, "run_id", pipeline)

    def set_provider_config(self, key_hash: str, provider: str, provider_api_key: str, model: str) -> None:
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                """SELECT 1
                     FROM api_keys AS credential
                     LEFT JOIN service_accounts AS account
                       ON account.organization_id=credential.organization_id
                      AND account.id=credential.service_account_id
                    WHERE credential.key_hash=?
                      AND credential.revoked_at=''
                      AND (credential.expires_at='' OR credential.expires_at>?)
                      AND (
                          credential.key_type<>'organization_service'
                          OR (
                              account.id IS NOT NULL
                              AND account.status='active'
                              AND account.revoked_at=''
                              AND (account.expires_at='' OR account.expires_at>?)
                          )
                      )""",
                (key_hash, _now(), _now()),
            ).fetchone()
            if not active:
                raise ValueError("provider configuration requires an active key")
            db.execute("INSERT INTO provider_config(key_hash,provider,provider_api_key,model) VALUES (?,?,?,?) ON CONFLICT(key_hash) DO UPDATE SET provider=excluded.provider,provider_api_key=excluded.provider_api_key,model=excluded.model",
                       (key_hash, provider, provider_api_key, model))

    def get_provider_config(self, key_hash: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT provider,provider_api_key,model FROM provider_config WHERE key_hash=?", (key_hash,)).fetchone()
        return None if not row else {"provider": row[0], "provider_api_key": row[1], "model": row[2]}

    def purge_provider_configs(self, limit: int = 500) -> int:
        batch_limit = max(1, min(int(limit), 1000))
        now = _now()
        with self._conn() as db:
            cursor = db.execute(
                """DELETE FROM provider_config
                    WHERE key_hash IN (
                        SELECT config.key_hash
                          FROM provider_config AS config
                          LEFT JOIN api_keys AS credential
                            ON credential.key_hash=config.key_hash
                          LEFT JOIN service_accounts AS account
                            ON account.organization_id=credential.organization_id
                           AND account.id=credential.service_account_id
                         WHERE credential.key_hash IS NULL
                            OR credential.revoked_at<>''
                            OR (credential.expires_at<>'' AND credential.expires_at<=?)
                            OR (
                                credential.key_type='organization_service'
                                AND (
                                    account.id IS NULL
                                    OR account.status<>'active'
                                    OR account.revoked_at<>''
                                    OR (account.expires_at<>'' AND account.expires_at<=?)
                                )
                            )
                         ORDER BY config.key_hash
                         LIMIT ?
                    )""",
                (now, now, batch_limit),
            )
        return max(0, int(cursor.rowcount or 0))


class SupabaseUsageStore:
    """Small PostgREST client; no extra Supabase SDK or mirror database."""

    def __init__(self, url: str | None = None, key: str | None = None,
                 *, cursor_secret: str | None = None):
        self.url = (url or os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "").rstrip("/")
        self.key = key or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
        self._company_cursor_secret = cursor_secret or os.getenv(
            "COMPANY_ADMIN_CURSOR_SECRET", ""
        )
        if not self.url or not self.key:
            raise ValueError("Supabase URL and service-role key are required")

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 data: Any = None, prefer: str = "return=representation") -> Any:
        started = time.perf_counter()
        outcome = "error"
        try:
            response = requests.request(
                method, f"{self.url}/rest/v1/{path}", params=params, json=data,
                headers={"apikey": self.key, "Authorization": f"Bearer {self.key}",
                         "Content-Type": "application/json", "Prefer": prefer}, timeout=10,
            )
            response.raise_for_status()
            outcome = "success"
            return response.json() if response.content else None
        except requests.Timeout:
            outcome = "timeout"
            raise
        except requests.ConnectionError:
            outcome = "unavailable"
            raise
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            outcome = "unavailable" if int(status or 0) >= 500 else "error"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            _record_postgres_dependency(outcome, time.perf_counter() - started)

    def healthy(self) -> bool:
        self._request("GET", "organizations", params={"select": "id", "limit": "1"})
        return True

    def ensure_organization(self, user_id: str, name: str = "",
                            account_type: str = "company") -> dict[str, Any]:
        if account_type not in {"individual", "company"}:
            raise ValueError("invalid account type")
        member = self._request("GET", "organization_members", params={
            "select": "organization_id", "user_id": f"eq.{user_id}", "limit": "1",
        }) or []
        if member:
            organization_id = member[0]["organization_id"]
            rows = self._request("GET", "organizations", params={
                "select": "id,name,billing_owner_id,account_type",
                "id": f"eq.{organization_id}", "limit": "1",
            }) or []
            return rows[0]
        rows = self._request("POST", "rpc/ensure_workspace_organization", data={
            "p_user_id": user_id, "p_name": name or "My organization",
            "p_account_type": account_type,
        }) or []
        return {"id": rows[0]["id"], "name": rows[0]["name"],
                "billing_owner_id": rows[0].get("billing_owner_id") or user_id,
                "account_type": rows[0]["account_type"]}

    def cache_enabled(self, organization_id: str, customer_id: str = "") -> bool:
        if not organization_id:
            return False
        if customer_id:
            rows = self._request("GET", "customers", params={
                "select": "cache_enabled", "id": f"eq.{customer_id}",
                "organization_id": f"eq.{organization_id}", "limit": "1",
            }) or []
            if rows and rows[0].get("cache_enabled") is True:
                return True
        rows = self._request("GET", "organizations", params={
            "select": "cache_enabled", "id": f"eq.{organization_id}", "limit": "1",
        }) or []
        return bool(rows and rows[0].get("cache_enabled") is True)

    def set_cache_enabled(self, organization_id: str, enabled: bool,
                          customer_id: str = "") -> None:
        table = "customers" if customer_id else "organizations"
        params = ({"id": f"eq.{customer_id}", "organization_id": f"eq.{organization_id}"}
                  if customer_id else {"id": f"eq.{organization_id}"})
        rows = self._request("PATCH", table, params=params,
                             data={"cache_enabled": bool(enabled)}) or []
        if not rows:
            raise ValueError("cache tenant not found")

    def member_organization(self, user_id: str) -> dict[str, Any] | None:
        try:
            actor_id = _required_uuid(user_id, "user_id")
        except ValueError:
            return None
        resolved = _rpc_object(self._request(
            "POST", "rpc/company_admin_resolve_active_membership", data={
                "p_actor_user_id": actor_id,
            },
        ))
        if resolved.pop("ok", False) is not True:
            return None
        if set(resolved) != {"company_id", "role"}:
            raise RuntimeError("active company resolver returned unsafe result")
        role = str(resolved.get("role") or "")
        if role not in COMPANY_ROLES:
            raise RuntimeError("active company resolver returned unsafe role")
        try:
            organization_id = _required_uuid(
                str(resolved.get("company_id") or ""), "organization_id")
        except ValueError as exc:
            raise RuntimeError("active company resolver returned unsafe tenant") from exc
        rows = self._request("GET", "organizations", params={
            "select": "id,name,billing_owner_id,account_type",
            "id": f"eq.{organization_id}", "limit": "1",
        }) or []
        return ({**rows[0], "role": role}) if rows else None

    def onboarding_status(self, user_id: str, organization_id: str) -> dict[str, Any]:
        actor_id = _required_uuid(user_id, "onboarding actor_user_id")
        organization_uuid = _required_uuid(
            organization_id, "onboarding organization_id")
        value = self._request(
            "POST", "rpc/organization_onboarding_status", data={
                "p_actor_user_id": actor_id,
                "p_organization_id": organization_uuid,
            },
        )
        return _validated_onboarding_status(value, organization_uuid)

    def complete_onboarding(self, user_id: str, organization_id: str,
                            request_id: str) -> dict[str, Any]:
        actor_id = _required_uuid(user_id, "onboarding actor_user_id")
        organization_uuid = _required_uuid(
            organization_id, "onboarding organization_id")
        _, audit_request_id, _ = _audit_identity(
            actor_id, request_id, "company_owner",
        )
        value = self._request(
            "POST", "rpc/complete_organization_onboarding", data={
                "p_actor_user_id": actor_id,
                "p_organization_id": organization_uuid,
                "p_request_id": audit_request_id,
            },
        )
        return _validated_onboarding_status(value, organization_uuid)

    def ensure_service_account(self, organization_id: str, environment: str,
                               created_by: str = "") -> dict[str, Any]:
        name = f"Company backend ({environment})"
        rows = self._request("GET", "service_accounts", params={
            "select": "id,name,environment", "organization_id": f"eq.{organization_id}",
            "name": f"eq.{name}", "environment": f"eq.{environment}", "limit": "1",
        }) or []
        if rows:
            return rows[0]
        rows = self._request("POST", "service_accounts", params={
            "on_conflict": "organization_id,name,environment",
        }, data={"organization_id": organization_id,
                 "name": name, "environment": environment, "created_by": created_by or None},
            prefer="resolution=merge-duplicates,return=representation") or []
        return rows[0]

    def upsert_customer(self, organization_id: str, external_id: str,
                        display_name: str = "") -> dict[str, Any]:
        return self.upsert_customers(organization_id, [{
            "external_id": external_id, "display_name": display_name,
        }])[0]

    def upsert_customers(self, organization_id: str,
                         customers: list[dict[str, str]]) -> list[dict[str, Any]]:
        if not customers:
            return []
        payload = [{"external_id": customer["external_id"],
                    "display_name": customer.get("display_name", "")}
                   for customer in customers]
        rows = self._request("POST", "rpc/import_enterprise_customers", data={
            "p_organization_id": organization_id, "p_customers": payload,
        }) or []
        if len(rows) != len(customers):
            raise RuntimeError(
                f"customer import returned {len(rows)} of {len(customers)} rows"
            )
        return rows

    def find_customer(self, organization_id: str, external_id: str) -> dict[str, Any] | None:
        rows = self._request("GET", "customers", params={
            "select": "id,external_id,display_name,status",
            "organization_id": f"eq.{organization_id}", "external_id": f"eq.{external_id}",
            "limit": "1",
        }) or []
        return rows[0] if rows else None

    def list_customers(self, organization_id: str) -> list[dict[str, Any]]:
        return self._request("GET", "customers", params={
            "select": "id,external_id,display_name,status,created_at,updated_at",
            "organization_id": f"eq.{organization_id}", "order": "created_at.desc",
        }) or []

    def create_device_request(self, device_hash: str, expires_at: str) -> None:
        self._request("DELETE", "bvx_device_auth", params={"expires_at": f"lt.{_now()}"})
        self._request("POST", "bvx_device_auth", data={"device_hash": device_hash,
                      "expires_at": expires_at})

    def get_device_request(self, device_hash: str) -> dict | None:
        if not _SHA256_DIGEST.fullmatch(str(device_hash or "")):
            raise ValueError("invalid device digest")
        value = _rpc_object(self._request(
            "POST", "rpc/get_bvx_device_exchange",
            data={"p_device_hash": device_hash},
        ))
        return value or None

    def resolve_device_approval_organization(
            self, owner_id: str,
            selected_organization_id: str = "") -> dict[str, str]:
        selected: str | None = None
        if selected_organization_id:
            try:
                selected = _required_uuid(
                    selected_organization_id, "selected_organization_id")
            except ValueError as exc:
                raise ValueError("company_access_denied") from exc
        result = _rpc_object(self._request(
            "POST", "rpc/resolve_bvx_device_approval_organization", data={
                "p_owner_id": owner_id,
                "p_selected_organization_id": selected,
            },
        ))
        if result.pop("ok", False) is not True:
            code = str(result.get("code") or "company_access_denied")
            if code not in ("company_selection_required", "company_access_denied"):
                code = "company_access_denied"
            raise ValueError(code)
        if set(result) != {"id", "role"} or result.get("role") not in COMPANY_ROLES:
            raise RuntimeError("device organization resolver returned unsafe result")
        try:
            result["id"] = _required_uuid(str(result["id"]), "organization_id")
        except ValueError as exc:
            raise RuntimeError(
                "device organization resolver returned invalid tenant") from exc
        return result

    def approve_device_request(self, device_hash: str, owner_id: str, key_hash: str,
                               encrypted_key: str,
                               organization_id: str = "") -> bool:
        resolved = self.resolve_device_approval_organization(
            owner_id, organization_id,
        )
        return bool(self._request("POST", "rpc/approve_bvx_device", data={
            "p_device_hash": device_hash, "p_owner_id": owner_id,
            "p_key_hash": key_hash, "p_encrypted_key": encrypted_key,
            "p_organization_id": resolved["id"],
        }))

    def consume_device_request(self, device_hash: str) -> dict | None:
        rows = self._request("POST", "rpc/consume_bvx_device",
                             data={"p_device_hash": device_hash}) or []
        return rows[0] if rows else None

    def consume_device_request_idempotent(self, device_hash: str,
                                          expected_key_hash: str,
                                          request_id: str) -> dict[str, Any] | None:
        """Call the atomic device activation RPC, retrying only ambiguous transport loss."""
        device_hash, expected_key_hash, request_id = _device_consume_identity(
            device_hash, expected_key_hash, request_id,
        )
        payload = {
            "p_device_hash": device_hash,
            "p_expected_key_hash": expected_key_hash,
            "p_request_id": request_id,
        }

        def consume() -> dict[str, Any]:
            return _rpc_object(self._request(
                "POST", "rpc/consume_bvx_device_idempotent", data=payload,
            ))

        try:
            response = consume()
        except (requests.Timeout, requests.ConnectionError):
            # The first transaction may have committed. The same request-bound
            # RPC returns its retained receipt and cannot mint a second key.
            response = consume()
        if not response:
            raise RuntimeError("device consume RPC returned no result")
        if response.pop("ok", False) is not True:
            code = str(response.get("code") or "device_consume_failed")
            if code == "expired_or_missing":
                return None
            raise RuntimeError(f"device consume rejected: {code}")
        return _validated_device_receipt(
            response, device_hash=device_hash,
            expected_key_hash=expected_key_hash,
        )

    def create_key(self, key_hash: str, name: str, owner_id: str = "", *,
                   organization_id: str = "", service_account_id: str = "",
                   key_type: str = "legacy", scopes: list[str] | None = None,
                   environment: str = "", key_prefix: str = "",
                   created_by: str = "", expires_at: str = "",
                   request_id: str = "", actor_role: str = "legacy") -> dict[str, Any]:
        """Create one dashboard key through the atomic database/audit boundary.

        Legacy digest/name arguments remain accepted so W1 can migrate callers without
        a flag day. They are never sent to PostgREST; this method owns raw-key generation.
        """
        if key_type != "dashboard_session" or service_account_id:
            raise ValueError("Supabase create_key supports dashboard_session only")
        organization_uuid = _required_uuid(organization_id, "organization_id")
        actor_uuid = _required_uuid(created_by, "actor_user_id")
        if not request_id:
            raise ValueError("request_id is required for atomic key creation")
        _, audit_request_id, resolved_role = _audit_identity(
            actor_uuid, request_id, actor_role,
        )
        if resolved_role not in ATOMIC_DASHBOARD_KEY_ROLES:
            raise ValueError("actor_role cannot create dashboard keys")
        expiry = _dashboard_expiry(expires_at)

        raw_key = generate_api_key()
        generated_hash = hash_key(raw_key)
        generated_prefix = raw_key[:12]
        try:
            result = _rpc_object(self._request(
                "POST", "rpc/company_admin_create_dashboard_session_key", data={
                    "p_organization_id": organization_uuid,
                    "p_actor_user_id": actor_uuid,
                    "p_key_hash": generated_hash,
                    "p_key_prefix": generated_prefix,
                    "p_expires_at": expiry,
                    "p_request_id": audit_request_id,
                },
            ))
        except Exception:
            raw_key = ""
            raise
        try:
            if result.get("ok") is not True:
                code = str(result.get("code") or "rejected")
                raise RuntimeError(f"atomic dashboard key creation failed: {code}")
            key_id = _required_uuid(str(result.get("key_id") or ""), "returned key_id")
            if str(result.get("organization_id") or "") != organization_uuid:
                raise RuntimeError("atomic dashboard key creation returned wrong tenant")
        except Exception:
            raw_key = ""
            raise
        return {
            "api_key": raw_key,
            "secret_available_once": True,
            "key_id": key_id,
            "organization_id": organization_uuid,
            "key_type": "dashboard_session",
            "scopes": list(result.get("scopes") or []),
            "environment": str(result.get("environment") or "dashboard"),
            "prefix": str(result.get("prefix") or generated_prefix),
            "expires_at": str(result.get("expires_at") or expiry),
            "request_id": audit_request_id,
        }

    def key_exists(self, key_hash: str) -> bool:
        return self.key_context(key_hash) is not None

    def key_context(self, key_hash: str) -> dict[str, Any] | None:
        rows = self._request("GET", "api_keys", params={
            "select": "key_hash,owner_id,organization_id,service_account_id,key_type,scopes,environment,expires_at,revoked_at",
            "key_hash": f"eq.{key_hash}", "revoked_at": "is.null", "limit": "1",
        }) or []
        if not rows:
            return None
        row = rows[0]
        expires_at = str(row.get("expires_at") or "")
        if expires_at and expires_at <= _now():
            return None
        self._request("PATCH", "api_keys", params={"key_hash": f"eq.{key_hash}"},
                      data={"last_used_at": _now()}, prefer="return=minimal")
        return row

    def key_owner(self, key_hash: str) -> str:
        rows = self._request("GET", "api_keys", params={
            "select": "owner_id,organization_id,key_type",
            "key_hash": f"eq.{key_hash}", "limit": "1",
        }) or []
        if not rows:
            return ""
        row = rows[0]
        if row.get("key_type") == "organization_service":
            organization_id = str(row.get("organization_id") or "")
            if not organization_id:
                return ""
            organizations = self._request("GET", "organizations", params={
                "select": "billing_owner_id", "id": f"eq.{organization_id}",
                "limit": "1",
            }) or []
            return (str(organizations[0].get("billing_owner_id") or "")
                    if organizations else "")
        return str(row.get("owner_id") or "")

    def list_keys(self, key_hash: str = "") -> list[dict[str, Any]]:
        params = {"select": "key_hash,name,created", "order": "created.desc"}
        owner = self.key_owner(key_hash) if key_hash else ""
        if owner:
            params["owner_id"] = f"eq.{owner}"
        elif key_hash:
            params["key_hash"] = f"eq.{key_hash}"
        return [{"id": row["key_hash"], "name": row["name"], "created": row["created"]}
                for row in (self._request("GET", "api_keys", params=params) or [])]

    def list_organization_keys(self, organization_id: str) -> list[dict[str, Any]]:
        raise RuntimeError(
            "Supabase key listing requires actor, request, and opaque cursor context"
        )

    def list_organization_keys_page(
        self, organization_id: str, actor_user_id: str, *, cursor: str = "",
        limit: int = 50, request_id: str, actor_role: str,
    ) -> dict[str, Any]:
        organization_uuid = _required_uuid(organization_id, "organization_id")
        actor_uuid = _required_uuid(actor_user_id, "actor_user_id")
        if not request_id:
            raise ValueError("request_id is required for dashboard key listing")
        _, audit_request_id, resolved_role = _audit_identity(
            actor_uuid, request_id, actor_role,
        )
        if resolved_role not in ATOMIC_DASHBOARD_KEY_ROLES:
            raise ValueError("actor_role cannot list dashboard keys")
        try:
            page_limit = min(max(int(limit), 1), 100)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid dashboard key page limit") from exc
        secret = _cursor_secret(self._company_cursor_secret)
        decoded = _decode_dashboard_cursor(secret, cursor, organization_uuid)
        result = _rpc_object(self._request(
            "POST", "rpc/company_admin_dashboard_keys_page", data={
                "p_organization_id": organization_uuid,
                "p_actor_user_id": actor_uuid,
                "p_cursor_time": decoded[0] if decoded else None,
                "p_cursor_id": decoded[1] if decoded else None,
                "p_limit": page_limit,
                "p_request_id": audit_request_id,
            },
        ))
        if result.get("ok") is not True:
            raise RuntimeError(
                f"dashboard key listing failed: {result.get('code') or 'rejected'}"
            )
        items = result.get("items")
        if not isinstance(items, list) or len(items) > page_limit + 1:
            raise RuntimeError("dashboard key RPC exceeded page contract")
        rows = [dict(row) if isinstance(row, dict) else {} for row in items]
        positions = [_dashboard_key_tuple(row) for row in rows]
        if any(left <= right for left, right in zip(positions, positions[1:])):
            raise RuntimeError("dashboard key RPC returned unstable ordering")
        if decoded and positions:
            cursor_time = datetime.fromisoformat(decoded[0].replace("Z", "+00:00"))
            cursor_position = (cursor_time.astimezone(timezone.utc), uuid.UUID(decoded[1]).int)
            if positions[0] >= cursor_position:
                raise RuntimeError("dashboard key RPC violated cursor boundary")
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        next_cursor = (_encode_dashboard_cursor(
            secret, organization_uuid, str(rows[-1]["created"]), str(rows[-1]["id"]),
        ) if has_more and rows else "")
        return {"keys": rows, "next_cursor": next_cursor,
                "has_more": has_more, "limit": page_limit}

    def revoke_organization_key(self, organization_id: str, target_key_id: str,
                                actor_user_id: str = "", request_id: str = "",
                                actor_role: str = "legacy") -> bool:
        organization_uuid = _required_uuid(organization_id, "organization_id")
        actor_uuid = _required_uuid(actor_user_id, "actor_user_id")
        key_uuid = _required_uuid(target_key_id, "key_id")
        if not request_id:
            raise ValueError("request_id is required for atomic key revocation")
        _, audit_request_id, resolved_role = _audit_identity(
            actor_uuid, request_id, actor_role,
        )
        if resolved_role not in ATOMIC_DASHBOARD_KEY_ROLES:
            raise ValueError("actor_role cannot revoke keys")
        result = _rpc_object(self._request(
            "POST", "rpc/company_admin_revoke_dashboard_session_key", data={
                "p_organization_id": organization_uuid,
                "p_actor_user_id": actor_uuid,
                "p_key_id": key_uuid,
                "p_request_id": audit_request_id,
            },
        ))
        if result.get("ok") is not True:
            raise RuntimeError(
                f"atomic key revocation failed: {result.get('code') or 'rejected'}"
            )
        if str(result.get("key_id") or "") != key_uuid:
            raise RuntimeError("atomic key revocation returned wrong key")
        return True

    def revoke_keys_by_type(self, organization_id: str, key_type: str,
                            actor_user_id: str = "") -> int:
        raise RuntimeError(
            "Supabase bulk key revocation is disabled; use an atomic audited key RPC"
        )

    def register_installation(self, organization_id: str, service_account_id: str,
                              installation_id: str, repository: str | None, environment: str,
                              bvx_version: str, device_fingerprint: str = "", *,
                              repository_id: str = "", device_platform: str = "",
                              device_arch: str = "", client_name: str = "",
                              registration_key_hash: str = "") -> dict[str, Any]:
        organization_uuid = _required_uuid(organization_id, "organization_id")
        installation_uuid = _required_uuid(installation_id, "installation_id")
        if not _SHA256_DIGEST.fullmatch(str(registration_key_hash or "")):
            raise ValueError("invalid installation credential")
        if repository is None:
            current = self._request("GET", "installations", params={
                "select": "repository_id,repository", "id": f"eq.{installation_id}",
                "organization_id": f"eq.{organization_id}", "limit": "1",
            }) or []
            repository_id = str(current[0].get("repository_id") or "") if current else ""
            repository = str(current[0].get("repository") or "") if current else ""
        result = _rpc_object(self._request(
            "POST", "rpc/register_bvx_installation", data={
                "p_organization_id": organization_uuid,
                "p_registration_key_hash": registration_key_hash,
                "p_installation_id": installation_uuid,
                "p_device_fingerprint": device_fingerprint,
                "p_repository_id": repository_id,
                "p_repository": repository or "",
                "p_environment": environment,
                "p_device_platform": device_platform,
                "p_device_arch": device_arch,
                "p_client_name": client_name,
                "p_bvx_version": bvx_version,
            },
        ))
        if result.get("ok") is not True:
            code = str(result.get("code") or "registration_failed")
            messages = {
                "invalid_request": "invalid installation request",
                "forbidden": "installation credential is not active",
                "device_revoked": "device is revoked",
                "foreign_installation": (
                    "installation id belongs to another organization"),
                "installation_revoked": "installation is revoked",
                "credential_mismatch": "installation credential mismatch",
            }
            raise ValueError(messages.get(code, "installation registration rejected"))
        return _validated_installation_registration(result, installation_uuid)

    def list_installations(self, organization_id: str) -> list[dict[str, Any]]:
        return self._request("GET", "installations", params={
            "select": "id,repository_id,repository,environment,device_platform,device_arch,client_name,bvx_version,installed_at,last_seen_at,revoked_at",
            "organization_id": f"eq.{organization_id}", "order": "last_seen_at.desc",
        }) or []

    def organization_inventory(self, organization_id: str) -> dict[str, Any]:
        members = self._request("GET", "organization_members", params={
            "select": "user_id,role,created_at", "organization_id": f"eq.{organization_id}",
            "order": "created_at.asc",
        }) or []
        devices = self._request("GET", "devices", params={
            "select": "id,display_name,created_at,last_seen_at,revoked_at",
            "organization_id": f"eq.{organization_id}", "order": "last_seen_at.desc",
        }) or []
        customers = self.list_customers(organization_id)
        keys = self.list_organization_keys(organization_id)
        installations = self.list_installations(organization_id)
        return {
            "counts": {"members": len(members), "customers": len(customers),
                       "keys": len(keys), "devices": len(devices),
                       "installations": len(installations)},
            "members": members, "customers": customers, "keys": keys,
            "devices": devices, "installations": installations,
        }

    def revoke_installation(self, organization_id: str, installation_id: str) -> bool:
        rows = self._request("PATCH", "installations", params={
            "id": f"eq.{installation_id}", "organization_id": f"eq.{organization_id}",
            "revoked_at": "is.null",
        }, data={"revoked_at": _now()}) or []
        return bool(rows)

    def register_repository(self, key_hash: str, repo: str, source: str = "bvx") -> None:
        now = _now()
        self._request("POST", "key_repositories", params={"on_conflict": "key_hash,repo"}, data={
            "key_hash": key_hash, "owner_id": self.key_owner(key_hash), "repo": repo,
            "source": source, "installed_at": now, "last_seen": now,
        }, prefer="resolution=merge-duplicates,return=representation")

    def get_admin_key_inventory(self) -> dict[str, Any]:
        keys = self._request("GET", "api_keys", params={
            "select": "key_hash,name,created,owner_id", "order": "created.desc",
            "limit": str(ADMIN_RESULT_MAX),
        }) or []
        repositories = self._request("GET", "key_repositories", params={
            "select": "key_hash,owner_id,repo,source,installed_at,last_seen",
            "order": "last_seen.desc", "limit": str(ADMIN_RESULT_MAX * 4),
        }) or []
        usage = self._request("POST", "rpc/admin_key_repository_usage", data={
            "p_limit": ADMIN_RESULT_MAX * 4,
        }) or []

        owner_ids = sorted({str(key.get("owner_id") or "") for key in keys
                            if key.get("owner_id")})
        emails: dict[str, str] = {}
        safe_ids = [owner for owner in owner_ids
                    if len(owner) == 36 and all(c.isalnum() or c == '-' for c in owner)]
        if safe_ids:
            profiles = self._request("GET", "profiles", params={
                "select": "id,email", "id": f"in.({','.join(safe_ids)})",
            }) or []
            emails = {str(profile.get("id")): str(profile.get("email") or "")
                      for profile in profiles}
        result = _admin_key_inventory(keys, repositories, usage, emails)
        result["truncated"] = len(keys) == ADMIN_RESULT_MAX
        return result

    def delete_key(self, current_key_hash: str, target_key_hash: str) -> bool:
        owner = self.key_owner(current_key_hash)
        rows = self._request("GET", "api_keys", params={"select": "key_hash,owner_id", "key_hash": f"eq.{target_key_hash}", "limit": "1"})
        allowed = bool(owner and rows and rows[0].get("owner_id") == owner)
        if not rows or (not allowed and target_key_hash != current_key_hash):
            return False
        self._request("DELETE", "api_keys", params={"key_hash": f"eq.{target_key_hash}"})
        return True

    def has_request(self, key_hash: str, request_id: str) -> bool:
        if not request_id:
            return False
        return bool(self._request("GET", "usage_log", params={"select": "id", "key_hash": f"eq.{key_hash}", "request_id": f"eq.{request_id}", "limit": "1"}))

    def record_usage(self, key_hash: str, baseline_tokens: int, optimized_tokens: int,
                     savings_pct: float = 0, quality_proxy: Optional[float] = None,
                     **values: Any) -> bool:
        row = _usage_row(key_hash, baseline_tokens, optimized_tokens, savings_pct, quality_proxy, **values)
        # Postgres tenant identifiers are nullable UUIDs. Historical/unattributed
        # traffic uses NULL, never the invalid UUID literal ''.
        row["organization_id"] = row.get("organization_id") or None
        row["customer_id"] = row.get("customer_id") or None
        result = self._request("POST", "usage_log", data=row,
                               prefer="return=representation,resolution=ignore-duplicates")
        return bool(result)

    def record_usage_batch(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Insert at most 100 receipts, isolating malformed/failed rows on retry."""
        if len(records) > USAGE_BATCH_MAX:
            raise ValueError(f"usage batch exceeds maximum of {USAGE_BATCH_MAX}")
        result = {"read": len(records), "inserted": 0, "duplicates": 0, "failed": 0}
        prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
        failed_records: list[dict[str, Any]] = []
        for record in records:
            try:
                row = _batch_record_row(record)
                row["organization_id"] = row.get("organization_id") or None
                row["customer_id"] = row.get("customer_id") or None
                prepared.append((dict(record), row))
            except (TypeError, ValueError):
                result["failed"] += 1
                failed_records.append(dict(record))
        if not prepared:
            if failed_records:
                result["failed_records"] = failed_records
            return result
        rows = [row for _, row in prepared]
        try:
            inserted = self._request(
                "POST", "usage_log", data=rows,
                prefer="return=representation,resolution=ignore-duplicates",
            ) or []
            result["inserted"] += len(inserted)
            result["duplicates"] += len(rows) - len(inserted)
            if failed_records:
                result["failed_records"] = failed_records
            return result
        except requests.RequestException as exc:
            stable_ids = all(str(row.get("request_id") or "") for row in rows)
            if not _definite_noncommit(exc) and not stable_ids:
                raise AmbiguousUsageBatchError(
                    "usage batch outcome is unknown and rows without request_id "
                    "cannot be retried safely", [original for original, _ in prepared],
                ) from exc

            retry_records: list[dict[str, Any]] = []
            ambiguous_records: list[dict[str, Any]] = []
            # A definite 4xx rejects the atomic multi-row transaction. An ambiguous
            # failure is isolated only when every row has a stable idempotency key.
            for original, row in prepared:
                try:
                    inserted = self._request(
                        "POST", "usage_log", data=row,
                        prefer="return=representation,resolution=ignore-duplicates",
                    ) or []
                    result["inserted" if inserted else "duplicates"] += 1
                except requests.RequestException as row_exc:
                    result["failed"] += 1
                    if _definite_noncommit(row_exc):
                        failed_records.append(original)
                    elif row.get("request_id"):
                        retry_records.append(original)
                    else:
                        # The single row may have committed. Preserve it for explicit
                        # reconciliation; automatic retry would duplicate an append-only row.
                        ambiguous_records.append(original)
            if failed_records:
                result["failed_records"] = failed_records
            if retry_records:
                result["retry_records"] = retry_records
            if ambiguous_records:
                result["ambiguous_records"] = ambiguous_records
            return result

    def _usage_scope(self, key_hash: str) -> dict[str, Any]:
        context = self.key_context(key_hash) or {}
        return {
            "p_key_hash": key_hash,
            "p_organization_id": context.get("organization_id") or None,
            "p_owner_id": str(context.get("owner_id") or ""),
        }

    def list_usage_page(self, key_hash: str, *, cursor: str = "",
                        limit: int = USAGE_PAGE_DEFAULT) -> dict[str, Any]:
        page_limit = _bounded_limit(limit)
        decoded = _decode_usage_cursor(cursor)
        data = {
            **self._usage_scope(key_hash),
            "p_cursor_ts": decoded[0] if decoded else None,
            "p_cursor_id": decoded[1] if decoded else None,
            "p_limit": page_limit,
        }
        rows = self._request("POST", "rpc/usage_page", data=data) or []
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        next_cursor = (_encode_usage_cursor(rows[-1]["ts"], rows[-1]["id"])
                       if has_more and rows else "")
        return {"rows": rows, "next_cursor": next_cursor, "limit": page_limit}

    def get_stats(self, key_hash: str) -> dict[str, Any]:
        result = self._request("POST", "rpc/usage_stats", data=self._usage_scope(key_hash)) or {}
        result["by_pipeline"] = self.get_stats_by_pipeline(key_hash)
        result["by_agent"] = self.get_stats_by_agent(key_hash)
        return result

    def get_breakdown(self, key_hash: str) -> list[dict[str, Any]]:
        return self._request("POST", "rpc/usage_breakdown", data={
            **self._usage_scope(key_hash), "p_limit": ADMIN_RESULT_MAX,
        }) or []

    def get_admin_stats(self) -> dict[str, Any]:
        report = self.get_admin_report({})
        return report["totals"]

    def get_admin_breakdown(self) -> list[dict[str, Any]]:
        report = self.get_admin_report({})
        return report["rows"]

    def get_admin_report(self, filters: dict[str, str]) -> dict[str, Any]:
        allowed = {field: filters[field] for field in (
            "start", "organization_id", "owner_id", "project", "client", "provider", "model"
        ) if filters.get(field)}
        report = self._request("POST", "rpc/admin_usage_report", data={
            "p_filters": allowed, "p_limit": ADMIN_RESULT_MAX,
        }) or {"totals": {}, "rows": []}
        rows = report.get("rows") or []
        owner_ids = sorted({str(row.get("account_id") or "") for row in rows
                            if row.get("account_id")})
        emails: dict[str, str] = {}
        if owner_ids:
            safe_ids = [owner for owner in owner_ids
                        if len(owner) == 36 and all(c.isalnum() or c == '-' for c in owner)]
            if safe_ids:
                profiles = self._request("GET", "profiles", params={
                    "select": "id,email", "id": f"in.({','.join(safe_ids)})",
                }) or []
                emails = {str(profile.get("id")): str(profile.get("email") or "")
                          for profile in profiles}
        for row in rows:
            row["account_email"] = emails.get(str(row.get("account_id") or ""), "")
        report["rows"] = rows
        return report

    def get_admin_report_page(self, filters: dict[str, str], *,
                              sort: str = "actual_cost_usd", direction: str = "desc",
                              cursor: str = "", limit: int = USAGE_PAGE_DEFAULT) -> dict[str, Any]:
        if sort not in ADMIN_SORT_FIELDS or direction not in ("asc", "desc"):
            raise ValueError("unsupported admin report ordering")
        page_limit = _bounded_limit(limit, ADMIN_RESULT_MAX)
        decoded = _decode_admin_cursor(cursor, sort, direction)
        allowed = {field: filters[field] for field in (
            "start", "organization_id", "owner_id", "project", "client", "provider", "model"
        ) if filters.get(field)}
        report = self._request("POST", "rpc/admin_usage_report_page", data={
            "p_filters": allowed, "p_sort": sort, "p_direction": direction,
            "p_cursor_value": decoded[0] if decoded else None,
            "p_cursor_key": decoded[1] if decoded else None,
            "p_limit": page_limit,
        }) or {"totals": {}, "rows": [], "total": 0}
        rows = report.get("rows") or []
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        next_cursor = (_encode_admin_cursor(
            sort, direction, rows[-1]["_sort_value"], rows[-1]["_row_key"]
        ) if has_more and rows else "")
        owner_ids = sorted({str(row.get("account_id") or "") for row in rows
                            if row.get("account_id")})
        emails: dict[str, str] = {}
        safe_ids = [owner for owner in owner_ids
                    if len(owner) == 36 and all(c.isalnum() or c == '-' for c in owner)]
        if safe_ids:
            profiles = self._request("GET", "profiles", params={
                "select": "id,email", "id": f"in.({','.join(safe_ids)})",
            }) or []
            emails = {str(profile.get("id")): str(profile.get("email") or "")
                      for profile in profiles}
        for row in rows:
            row["account_email"] = emails.get(str(row.get("account_id") or ""), "")
            row.pop("_sort_value", None)
            row.pop("_row_key", None)
        return {"rows": rows, "totals": report.get("totals") or {},
                "pagination": {"total": int(report.get("total") or 0),
                               "limit": page_limit, "next_cursor": next_cursor,
                               "has_more": has_more},
                "sort": sort, "direction": direction}

    def _usage_group(self, key_hash: str, field: str, pipeline: str = "",
                     start: str = "", end: str = "") -> list[dict[str, Any]]:
        return self._request("POST", "rpc/usage_grouped", data={
            **self._usage_scope(key_hash), "p_field": field,
            "p_pipeline": pipeline or None, "p_start": start or None,
            "p_end": end or None, "p_limit": ADMIN_RESULT_MAX,
        }) or []

    def get_stats_by_pipeline(self, key_hash: str, start: str = "", end: str = "", **_ignored) -> list:
        return self._usage_group(key_hash, "pipeline", start=start, end=end)

    def get_stats_by_agent(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "", **_ignored) -> list:
        return self._usage_group(key_hash, "agent", pipeline, start, end)

    def get_stats_by_run(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "") -> list:
        return self._usage_group(key_hash, "run_id", pipeline, start, end)

    def set_provider_config(self, key_hash: str, provider: str, provider_api_key: str, model: str) -> None:
        self._request("POST", "provider_config", data={"key_hash": key_hash, "provider": provider,
                      "provider_api_key": provider_api_key, "model": model}, prefer="resolution=merge-duplicates")

    def get_provider_config(self, key_hash: str) -> dict | None:
        rows = self._request("GET", "provider_config", params={"select": "provider,provider_api_key,model", "key_hash": f"eq.{key_hash}", "limit": "1"})
        return rows[0] if rows else None

    def purge_provider_configs(self, limit: int = 500) -> int:
        batch_limit = max(1, min(int(limit), 1000))
        result = self._request(
            "POST", "rpc/purge_expired_provider_configs",
            data={"p_limit": batch_limit},
        )
        if isinstance(result, list):
            result = result[0] if result else 0
        if isinstance(result, dict):
            result = next(iter(result.values()), 0)
        return int(result or 0)


class BoundedUsageWriter:
    """Time/size-bounded receipt buffer for non-authoritative telemetry.

    Authoritative receipts bypass the buffer after pending telemetry is flushed, so
    the Postgres billing trigger remains synchronous with the request that earned it.
    """

    def __init__(self, store: Any, *, max_batch_size: int = USAGE_BATCH_MAX,
                 flush_interval_seconds: float = 1.0, autostart: bool = True):
        if not 1 <= int(max_batch_size) <= USAGE_BATCH_MAX:
            raise ValueError(f"max_batch_size must be between 1 and {USAGE_BATCH_MAX}")
        if not 0.05 <= float(flush_interval_seconds) <= 60.0:
            raise ValueError("flush_interval_seconds must be between 0.05 and 60")
        self.store = store
        self.max_batch_size = int(max_batch_size)
        self.flush_interval_seconds = float(flush_interval_seconds)
        self._pending: list[dict[str, Any]] = []
        self._inflight: list[dict[str, Any]] = []
        self._unresolved: list[dict[str, Any]] = []
        self._condition = threading.Condition(threading.Lock())
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._closing = False
        self._closed = False
        self._active_adds = 0
        self._last_error: Exception | None = None
        self._thread: threading.Thread | None = None
        if autostart:
            self._thread = threading.Thread(
                target=self._run, name="brevitas-usage-flush", daemon=True,
            )
            self._thread.start()
        atexit.register(self.close, False)

    @property
    def pending_count(self) -> int:
        """Rows consuming capacity, including in-flight and unresolved outcomes."""
        with self._condition:
            return len(self._pending) + len(self._inflight) + len(self._unresolved)

    @property
    def failed_records(self) -> list[dict[str, Any]]:
        with self._condition:
            return [dict(record) for record in self._unresolved]

    def take_failed_records(self) -> list[dict[str, Any]]:
        """Transfer unresolved rows to a dead-letter/reconciliation owner."""
        with self._condition:
            records, self._unresolved = self._unresolved, []
            if not self._pending and not self._inflight:
                self._last_error = None
            self._condition.notify_all()
        return records

    def add(self, record: dict[str, Any]) -> dict[str, int] | None:
        with self._condition:
            if self._closed or self._closing:
                raise RuntimeError("usage writer is closing or closed")
            self._active_adds += 1
        try:
            if record.get("authoritative"):
                values = dict(record)
                key_hash = values.pop("key_hash")
                baseline_tokens = values.pop("baseline_tokens")
                optimized_tokens = values.pop("optimized_tokens")
                savings_pct = values.pop("savings_pct", 0)
                quality_proxy = values.pop("quality_proxy", None)
                inserted = self.store.record_usage(
                    key_hash, baseline_tokens, optimized_tokens, savings_pct,
                    quality_proxy, **values,
                )
                return {"read": 1, "inserted": int(bool(inserted)),
                        "duplicates": int(not bool(inserted)), "failed": 0}
            with self._condition:
                used = len(self._pending) + len(self._inflight) + len(self._unresolved)
                if used >= self.max_batch_size:
                    raise RuntimeError("usage buffer is full")
                self._pending.append(dict(record))
                full = used + 1 >= self.max_batch_size and not self._inflight
            return self.flush() if full else None
        finally:
            with self._condition:
                self._active_adds -= 1
                self._condition.notify_all()

    def flush(self) -> dict[str, Any]:
        with self._flush_lock:
            with self._condition:
                if not self._pending:
                    return {"read": 0, "inserted": 0, "duplicates": 0, "failed": 0}
                batch, self._pending = self._pending, []
                self._inflight = batch
            try:
                result = self.store.record_usage_batch(batch)
            except AmbiguousUsageBatchError as exc:
                with self._condition:
                    self._unresolved.extend(self._inflight)
                    self._inflight = []
                    self._last_error = exc
                    self._condition.notify_all()
                raise
            except Exception as exc:
                with self._condition:
                    self._pending = self._inflight + self._pending
                    self._inflight = []
                    self._last_error = exc
                    self._condition.notify_all()
                raise
            failed = int(result.get("failed") or 0)
            if failed:
                retry = [dict(row) for row in result.get("retry_records") or []]
                unresolved = [dict(row) for key in ("failed_records", "ambiguous_records")
                              for row in (result.get(key) or [])]
                if len(retry) + len(unresolved) != failed:
                    # The store did not identify failed rows. Retain the exact batch
                    # without retrying any member; successful rows must not be duplicated.
                    retry = []
                    unresolved = [dict(row) for row in self._inflight]
                error = UsageBatchPartialFailure(
                    f"usage batch had {failed} failed row(s); reconciliation required",
                    result,
                )
                with self._condition:
                    self._pending = retry + self._pending
                    self._unresolved.extend(unresolved)
                    self._inflight = []
                    self._last_error = error
                    self._condition.notify_all()
                raise error
            with self._condition:
                self._inflight = []
                if not self._unresolved:
                    self._last_error = None
                self._condition.notify_all()
            return result

    def _run(self) -> None:
        while not self._stop.wait(self.flush_interval_seconds):
            try:
                self.flush()
            except Exception:
                # Preserve the bounded batch for the next interval/shutdown. The
                # request path sees a full-buffer error rather than dropping rows.
                continue

    def close(self, raise_errors: bool = True) -> dict[str, int]:
        with self._condition:
            while self._closing and not self._closed:
                self._condition.wait()
            if self._closed:
                if raise_errors and self._last_error:
                    raise self._last_error
                return {"read": 0, "inserted": 0, "duplicates": 0,
                        "failed": len(self._unresolved)}
            self._closing = True
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=min(self.flush_interval_seconds + 0.1, 2.0))
        try:
            with self._condition:
                while self._active_adds:
                    self._condition.wait()
            result = self.flush()
            with self._condition:
                if self._unresolved:
                    raise self._last_error or UsageBatchPartialFailure(
                        "unresolved usage rows remain at shutdown"
                    )
                self._closed = True
                self._closing = False
                self._condition.notify_all()
            return result
        except Exception as exc:
            with self._condition:
                self._last_error = exc
                if not raise_errors:
                    self._closed = True
                self._closing = False
                remaining = len(self._pending) + len(self._inflight) + len(self._unresolved)
                self._condition.notify_all()
            if raise_errors:
                raise
            return {"read": 0, "inserted": 0, "duplicates": 0,
                    "failed": remaining}

    def __enter__(self) -> "BoundedUsageWriter":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close(raise_errors=exc is None)


def make_store():
    backend = os.getenv("BREVITAS_STORE", "").lower()
    configured = bool((os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL"))
                      and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    hosted = hosted_runtime()
    if hosted and (backend != "supabase" or not configured):
        raise RuntimeError("Production requires BREVITAS_STORE=supabase and Supabase credentials")
    if backend == "supabase" or (backend != "sqlite" and configured):
        return SupabaseUsageStore()
    return UsageStore(os.getenv("BREVITAS_SQLITE_PATH") or None)
