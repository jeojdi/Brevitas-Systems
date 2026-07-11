"""Idempotent one-time import from a legacy Brevitas SQLite usage_log."""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

from .store import make_store


def import_sqlite(path: str, target=None) -> dict[str, int]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    target = target or make_store()
    with sqlite3.connect(source) as db:
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute("pragma table_info(usage_log)")}
        if not columns:
            raise ValueError("usage_log table not found")
        rows = db.execute("select * from usage_log order by id").fetchall()

    inserted = duplicates = 0
    for raw in rows:
        row = dict(raw)
        fingerprint = "|".join(str(row.get(name, "")) for name in (
            "key_hash", "id", "ts", "provider", "model", "baseline_tokens", "optimized_tokens"
        ))
        request_id = row.get("request_id") or "sqlite:" + hashlib.sha256(fingerprint.encode()).hexdigest()
        ok = target.record_usage(
            key_hash=row.get("key_hash") or "",
            owner_id=row.get("owner_id") or "",
            ts=row.get("ts"),
            baseline_tokens=int(row.get("baseline_tokens") or 0),
            optimized_tokens=int(row.get("optimized_tokens") or 0),
            savings_pct=float(row.get("savings_pct") or 0),
            quality_proxy=row.get("quality_proxy"),
            project=row.get("project") or row.get("repo") or row.get("pipeline") or "Unattributed",
            environment=row.get("environment") or "Unattributed",
            source=row.get("source") or row.get("client") or "Unattributed",
            repo=row.get("repo") or "",
            client=row.get("client") or "",
            pipeline=row.get("pipeline") or "",
            agent=row.get("agent") or "",
            call_site_id=row.get("call_site_id") or "",
            framework=row.get("framework") or "",
            gateway=row.get("gateway") or "",
            run_id=row.get("run_id") or "",
            provider=row.get("provider") or "",
            model=row.get("model") or "",
            operation=row.get("operation") or "chat",
            fresh_input_tokens=int(row.get("fresh_input_tokens") or row.get("optimized_tokens") or 0),
            cached_input_tokens=int(row.get("cached_input_tokens") or row.get("cached_tokens") or 0),
            cache_write_tokens=int(row.get("cache_write_tokens") or 0),
            output_tokens=int(row.get("output_tokens") or 0),
            baseline_cost_usd=row.get("baseline_cost_usd"),
            actual_cost_usd=row.get("actual_cost_usd"),
            measured_savings_usd=(row.get("measured_savings_usd")
                                  if row.get("measured_savings_usd") is not None
                                  else row.get("cost_saved_usd")),
            verified_savings_usd=(row.get("verified_savings_usd")
                                  if row.get("verified_savings_usd") is not None
                                  else row.get("cost_saved_usd") or 0),
            brevitas_fee_usd=row.get("brevitas_fee_usd") or 0,
            quality_status=row.get("quality_status") or "historical",
            pricing_status=row.get("pricing_status") or ("priced" if row.get("cost_saved_usd") else "unpriced"),
            pricing_version=row.get("pricing_version") or "historical",
            strategy=row.get("strategy") or "historical",
            session_id=row.get("session_id") or "",
            request_id=request_id,
            receipt_source="import",
            is_stream=bool(row.get("is_stream")),
        )
        inserted += bool(ok)
        duplicates += not bool(ok)
    return {"read": len(rows), "inserted": inserted, "duplicates": duplicates}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_db")
    args = parser.parse_args()
    result = import_sqlite(args.sqlite_db)
    print(f"read={result['read']} inserted={result['inserted']} duplicates={result['duplicates']}")


if __name__ == "__main__":
    main()
