"""Idempotent one-time import from a legacy Brevitas SQLite usage_log."""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

from brevitas.proxy import RECEIPT_ID_PREFIX

from .store import USAGE_BATCH_MAX, make_store

# Same reservation the /v1/usage intake applies (api/server.py
# _CLIENT_REQUEST_ID_PREFIX). Kept as its own constant so this module does not
# import api.server (which mounts the proxy app and every route).
_CLIENT_REQUEST_ID_PREFIX = "client:"


def _reserved_namespace_safe(request_id: str) -> str:
    """Keep an imported id out of the server-minted metering namespace.

    RECEIPT_ID_PREFIX is RESERVED, not merely conventional: usage_log carries a
    unique index on (key_hash, request_id) where request_id <> ''
    (20260710_cloud_usage.sql:125) and that index does NOT include the
    `authoritative` column. A legacy SQLite usage_log can contain rows whose
    request_id is a `proxy:` id — older local proxies minted them, and the SDK
    still yields one to the client in the first SSE chunk — so importing that
    file verbatim would permanently occupy those slots with authoritative=False
    analytics rows. Every matching authoritative receipt written afterwards by
    the hosted bridge would then be silently swallowed as a duplicate: full
    optimization, zero billed. That is the same metering-suppression class the
    audit's critical finding covered, reached through an operator import instead
    of an HTTP header.

    Rewrite rather than reject, exactly as POST /v1/usage does: the prefix swap
    is deterministic, so re-running an interrupted import still dedupes against
    the rows the previous run wrote.
    """
    value = str(request_id or "")
    if value.startswith(RECEIPT_ID_PREFIX):
        value = _CLIENT_REQUEST_ID_PREFIX + value[len(RECEIPT_ID_PREFIX):]
    return value[:128]


def _import_record(row: dict) -> dict:
    fingerprint = "|".join(str(row.get(name, "")) for name in (
        "key_hash", "id", "ts", "provider", "model", "baseline_tokens", "optimized_tokens"
    ))
    request_id = _reserved_namespace_safe(
        row.get("request_id") or "sqlite:" + hashlib.sha256(fingerprint.encode()).hexdigest())
    return {
        "key_hash": row.get("key_hash") or "",
        "owner_id": row.get("owner_id") or "",
        "organization_id": row.get("organization_id") or "",
        "customer_id": row.get("customer_id") or "",
        # Historical receipts are analytics-only even when an old local schema
        # incorrectly marked them authoritative. Imports must never trigger billing.
        "authoritative": False,
        "ts": row.get("ts"),
        "baseline_tokens": int(row.get("baseline_tokens") or 0),
        "optimized_tokens": int(row.get("optimized_tokens") or 0),
        "savings_pct": float(row.get("savings_pct") or 0),
        "quality_proxy": row.get("quality_proxy"),
        "project": row.get("project") or row.get("repo") or row.get("pipeline") or "Unattributed",
        "environment": row.get("environment") or "Unattributed",
        "source": row.get("source") or row.get("client") or "Unattributed",
        "repo": row.get("repo") or "",
        "client": row.get("client") or "",
        "pipeline": row.get("pipeline") or "",
        "agent": row.get("agent") or "",
        "call_site_id": row.get("call_site_id") or "",
        "framework": row.get("framework") or "",
        "gateway": row.get("gateway") or "",
        "run_id": row.get("run_id") or "",
        "provider": row.get("provider") or "",
        "model": row.get("model") or "",
        "operation": row.get("operation") or "chat",
        "fresh_input_tokens": int(row.get("fresh_input_tokens") or row.get("optimized_tokens") or 0),
        "cached_input_tokens": int(row.get("cached_input_tokens") or row.get("cached_tokens") or 0),
        "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
        "cache_write_5m_tokens": int(row.get("cache_write_5m_tokens") or 0),
        "cache_write_1h_tokens": int(row.get("cache_write_1h_tokens") or 0),
        "cache_attributable": bool(row.get("cache_attributable")),
        "output_tokens": int(row.get("output_tokens") or 0),
        "baseline_cost_usd": row.get("baseline_cost_usd"),
        "actual_cost_usd": row.get("actual_cost_usd"),
        "measured_savings_usd": (row.get("measured_savings_usd")
                                  if row.get("measured_savings_usd") is not None
                                  else row.get("cost_saved_usd")),
        "verified_savings_usd": (row.get("verified_savings_usd")
                                  if row.get("verified_savings_usd") is not None
                                  else row.get("cost_saved_usd") or 0),
        # Imported history may retain savings analytics, but never a collectible fee.
        "brevitas_fee_usd": 0,
        "quality_status": row.get("quality_status") or "historical",
        "pricing_status": row.get("pricing_status") or ("priced" if row.get("cost_saved_usd") else "unpriced"),
        "pricing_version": row.get("pricing_version") or "historical",
        "strategy": row.get("strategy") or "historical",
        "session_id": row.get("session_id") or "",
        "request_id": request_id,
        "receipt_source": "import",
        "is_stream": bool(row.get("is_stream")),
    }


def import_sqlite(path: str, target=None, *, batch_size: int = USAGE_BATCH_MAX) -> dict[str, int]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    target = target or make_store()
    if not 1 <= int(batch_size) <= USAGE_BATCH_MAX:
        raise ValueError(f"batch_size must be between 1 and {USAGE_BATCH_MAX}")
    inserted = duplicates = read = 0
    with sqlite3.connect(source) as db:
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute("pragma table_info(usage_log)")}
        if not columns:
            raise ValueError("usage_log table not found")
        cursor = db.execute("select * from usage_log order by id")
        while True:
            rows = cursor.fetchmany(int(batch_size))
            if not rows:
                break
            records = [_import_record(dict(row)) for row in rows]
            read += len(records)
            if hasattr(target, "record_usage_batch"):
                result = target.record_usage_batch(records)
                inserted += result["inserted"]
                duplicates += result["duplicates"]
                if result.get("failed"):
                    raise RuntimeError(f"failed to import {result['failed']} usage rows")
            else:
                for record in records:
                    ok = target.record_usage(**record)
                    inserted += bool(ok)
                    duplicates += not bool(ok)
    return {"read": read, "inserted": inserted, "duplicates": duplicates}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_db")
    args = parser.parse_args()
    result = import_sqlite(args.sqlite_db)
    print(f"read={result['read']} inserted={result['inserted']} duplicates={result['duplicates']}")


if __name__ == "__main__":
    main()
