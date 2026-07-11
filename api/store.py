"""Authoritative usage stores: Supabase in cloud, SQLite for offline work/tests."""
from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import requests

from brevitas.receipts import MODEL_PRICES, canonical_provider


PROVIDER_COSTS_PER_1M: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
for (provider, model), prices in MODEL_PRICES.items():
    PROVIDER_COSTS_PER_1M[provider][model] = dict(prices)
PROVIDER_COSTS_PER_1M = dict(PROVIDER_COSTS_PER_1M)


def infer_provider(model: str, given: str = "") -> str:
    return canonical_provider(given, model)


def cost_for_tokens(provider: str, model: str, tokens: int) -> float:
    price = MODEL_PRICES.get((canonical_provider(provider, model), model))
    return 0.0 if not price else max(0, tokens) * price["input"] / 1_000_000


_USAGE_COLUMNS: dict[str, str] = {
    "owner_id": "TEXT NOT NULL DEFAULT ''",
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
    "output_tokens": "INTEGER NOT NULL DEFAULT 0",
    "baseline_cost_usd": "REAL",
    "actual_cost_usd": "REAL",
    "measured_savings_usd": "REAL",
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
        "output_tokens": int(values.get("output_tokens") or 0),
        "baseline_cost_usd": values.get("baseline_cost_usd"),
        "actual_cost_usd": values.get("actual_cost_usd"),
        "measured_savings_usd": values.get("measured_savings_usd"),
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


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = sum(_i(r.get("baseline_tokens")) for r in rows)
    optimized = sum(_i(r.get("optimized_tokens")) for r in rows)
    saved = sum(_i(r.get("tokens_saved")) for r in rows)
    measured = sum(_f(r.get("measured_savings_usd")) for r in rows)
    verified = sum(_f(r.get("verified_savings_usd", r.get("cost_saved_usd"))) for r in rows)
    actual_cost = sum(_f(r.get("actual_cost_usd")) for r in rows)
    baseline_cost = sum(_f(r.get("baseline_cost_usd")) for r in rows)
    fee = sum(_f(r.get("brevitas_fee_usd")) for r in rows)
    quality = [float(r["quality_proxy"]) for r in rows if r.get("quality_proxy") is not None]
    months: dict[str, dict[str, Any]] = {}
    for row in rows:
        month = str(row.get("ts") or "")[:7]
        bucket = months.setdefault(month, {"month": month, "calls": 0, "tokens_saved": 0,
            "measured_savings_usd": 0.0, "verified_savings_usd": 0.0,
            "cost_saved_usd": 0.0, "brevitas_fee_usd": 0.0})
        bucket["calls"] += 1
        bucket["tokens_saved"] += _i(row.get("tokens_saved"))
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
            "verified_savings_usd": _f(r.get("verified_savings_usd", r.get("cost_saved_usd"))),
            "cost_saved_usd": _f(r.get("verified_savings_usd", r.get("cost_saved_usd"))),
            "pricing_status": r.get("pricing_status") or "unpriced",
        } for r in history],
        "billing_by_month": [months[k] for k in sorted(months, reverse=True)[:12]],
    }


_BREAKDOWN_FIELDS = ("project", "environment", "source", "client", "agent",
                     "call_site_id", "framework", "gateway", "provider", "model", "operation")


def _breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(f) or ("Unattributed" if f in ("project", "environment", "source", "client") else "")
                     for f in _BREAKDOWN_FIELDS)].append(row)
    out = []
    for key, items in groups.items():
        stat = _stats(items)
        out.append({**dict(zip(_BREAKDOWN_FIELDS, key)),
                    "calls": stat["total_calls"],
                    "baseline_tokens": stat["total_baseline_tokens"],
                    "optimized_tokens": stat["total_optimized_tokens"],
                    "actual_tokens": stat["total_actual_tokens"],
                    "tokens_saved": stat["total_tokens_saved"],
                    "measured_savings_usd": stat["total_measured_savings_usd"],
                    "verified_savings_usd": stat["total_verified_savings_usd"],
                    "unpriced_calls": stat["unpriced_calls"]})
    return sorted(out, key=lambda r: (-r["tokens_saved"], r["project"], r["source"], r["model"]))


def _admin_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accounts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        accounts[str(row.get("owner_id") or "Unattributed")].append(row)
    return [{"account_id": account, **item} for account, account_rows in accounts.items()
            for item in _breakdown(account_rows)]


class UsageStore:
    """SQLite development/test fallback with the same public methods as Supabase."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(Path(__file__).parent / "brevitas.db")
        self._init()

    def _conn(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def _init(self) -> None:
        with self._conn() as db:
            db.execute("CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT '')")
            key_cols = {r[1] for r in db.execute("PRAGMA table_info(api_keys)")}
            if "owner_id" not in key_cols:
                db.execute("ALTER TABLE api_keys ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE TABLE IF NOT EXISTS provider_config (key_hash TEXT PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'ollama', provider_api_key TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT 'llama3.2')")
            db.execute("CREATE TABLE IF NOT EXISTS bvx_device_auth (device_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT '', key_hash TEXT NOT NULL DEFAULT '', encrypted_key TEXT NOT NULL DEFAULT '', approved_at TEXT NOT NULL DEFAULT '')")
            device_cols = {r[1] for r in db.execute("PRAGMA table_info(bvx_device_auth)")}
            if "key_hash" not in device_cols:
                db.execute("ALTER TABLE bvx_device_auth ADD COLUMN key_hash TEXT NOT NULL DEFAULT ''")
            definitions = ",\n".join(f"{name} {definition}" for name, definition in _USAGE_COLUMNS.items())
            db.execute(f"CREATE TABLE IF NOT EXISTS usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT NOT NULL, ts TEXT NOT NULL, {definitions})")
            existing = {r[1] for r in db.execute("PRAGMA table_info(usage_log)")}
            for name, definition in _USAGE_COLUMNS.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE usage_log ADD COLUMN {name} {definition}")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS usage_request_unique ON usage_log(key_hash, request_id) WHERE request_id <> ''")
            for column in ("ts", "project", "source", "repo", "client", "provider", "model", "call_site_id"):
                db.execute(f"CREATE INDEX IF NOT EXISTS usage_{column}_idx ON usage_log(key_hash, {column})")
            db.execute("UPDATE usage_log SET measured_savings_usd=cost_saved_usd, verified_savings_usd=cost_saved_usd WHERE measured_savings_usd IS NULL")

    def create_device_request(self, device_hash: str, expires_at: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM bvx_device_auth WHERE expires_at<=?", (_now(),))
            db.execute("INSERT INTO bvx_device_auth(device_hash,expires_at) VALUES (?,?)",
                       (device_hash, expires_at))

    def get_device_request(self, device_hash: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT device_hash,expires_at,owner_id,key_hash,encrypted_key,approved_at FROM bvx_device_auth WHERE device_hash=?",
                             (device_hash,)).fetchone()
        return dict(row) if row else None

    def approve_device_request(self, device_hash: str, owner_id: str, key_hash: str,
                               encrypted_key: str) -> bool:
        with self._conn() as db:
            cur = db.execute("UPDATE bvx_device_auth SET owner_id=?,key_hash=?,encrypted_key=?,approved_at=? WHERE device_hash=? AND approved_at='' AND expires_at>?",
                             (owner_id, key_hash, encrypted_key, _now(), device_hash, _now()))
        return bool(cur.rowcount)

    def consume_device_request(self, device_hash: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT owner_id,key_hash,encrypted_key FROM bvx_device_auth WHERE device_hash=? AND approved_at<>'' AND expires_at>?",
                             (device_hash, _now())).fetchone()
            if not row:
                return None
            db.execute("INSERT INTO api_keys(key_hash,name,created,owner_id) VALUES (?,?,?,?)",
                       (row["key_hash"], "bvx", _now(), row["owner_id"]))
            db.execute("DELETE FROM bvx_device_auth WHERE device_hash=?", (device_hash,))
        return dict(row)

    def create_key(self, key_hash: str, name: str, owner_id: str = "") -> None:
        with self._conn() as db:
            db.execute("INSERT OR IGNORE INTO api_keys(key_hash,name,created,owner_id) VALUES (?,?,?,?)",
                       (key_hash, name, _now(), owner_id))

    def key_exists(self, key_hash: str) -> bool:
        with self._conn() as db:
            return db.execute("SELECT 1 FROM api_keys WHERE key_hash=?", (key_hash,)).fetchone() is not None

    def key_owner(self, key_hash: str) -> str:
        with self._conn() as db:
            row = db.execute("SELECT owner_id FROM api_keys WHERE key_hash=?", (key_hash,)).fetchone()
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
                cur = db.execute(f"INSERT OR IGNORE INTO usage_log ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                                 [row[c] for c in columns])
            return bool(cur.rowcount)
        except sqlite3.IntegrityError:
            return False

    def _rows(self, key_hash: str) -> list[dict[str, Any]]:
        owner = self.key_owner(key_hash)
        with self._conn() as db:
            if owner:
                query = "SELECT * FROM usage_log WHERE owner_id=? OR key_hash=?"
                return [dict(r) for r in db.execute(query, (owner, key_hash))]
            return [dict(r) for r in db.execute("SELECT * FROM usage_log WHERE key_hash=?", (key_hash,))]

    def _all_rows(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            return [dict(r) for r in db.execute("SELECT * FROM usage_log")]

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
            db.execute("INSERT INTO provider_config(key_hash,provider,provider_api_key,model) VALUES (?,?,?,?) ON CONFLICT(key_hash) DO UPDATE SET provider=excluded.provider,provider_api_key=excluded.provider_api_key,model=excluded.model",
                       (key_hash, provider, provider_api_key, model))

    def get_provider_config(self, key_hash: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT provider,provider_api_key,model FROM provider_config WHERE key_hash=?", (key_hash,)).fetchone()
        return None if not row else {"provider": row[0], "provider_api_key": row[1], "model": row[2]}


class SupabaseUsageStore:
    """Small PostgREST client; no extra Supabase SDK or mirror database."""

    def __init__(self, url: str | None = None, key: str | None = None):
        self.url = (url or os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "").rstrip("/")
        self.key = key or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
        if not self.url or not self.key:
            raise ValueError("Supabase URL and service-role key are required")

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 data: Any = None, prefer: str = "return=representation") -> Any:
        response = requests.request(method, f"{self.url}/rest/v1/{path}", params=params, json=data,
            headers={"apikey": self.key, "Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json", "Prefer": prefer}, timeout=10)
        response.raise_for_status()
        return response.json() if response.content else None

    def create_device_request(self, device_hash: str, expires_at: str) -> None:
        self._request("DELETE", "bvx_device_auth", params={"expires_at": f"lt.{_now()}"})
        self._request("POST", "bvx_device_auth", data={"device_hash": device_hash,
                      "expires_at": expires_at})

    def get_device_request(self, device_hash: str) -> dict | None:
        rows = self._request("GET", "bvx_device_auth", params={"select": "*",
                             "device_hash": f"eq.{device_hash}", "limit": "1"}) or []
        return rows[0] if rows else None

    def approve_device_request(self, device_hash: str, owner_id: str, key_hash: str,
                               encrypted_key: str) -> bool:
        return bool(self._request("POST", "rpc/approve_bvx_device", data={
            "p_device_hash": device_hash, "p_owner_id": owner_id,
            "p_key_hash": key_hash, "p_encrypted_key": encrypted_key,
        }))

    def consume_device_request(self, device_hash: str) -> dict | None:
        rows = self._request("POST", "rpc/consume_bvx_device",
                             data={"p_device_hash": device_hash}) or []
        return rows[0] if rows else None

    def create_key(self, key_hash: str, name: str, owner_id: str = "") -> None:
        self._request("POST", "api_keys", data={"key_hash": key_hash, "name": name,
                      "owner_id": owner_id, "created": _now()}, prefer="resolution=ignore-duplicates")

    def key_exists(self, key_hash: str) -> bool:
        return bool(self._request("GET", "api_keys", params={"select": "key_hash", "key_hash": f"eq.{key_hash}", "limit": "1"}))

    def key_owner(self, key_hash: str) -> str:
        rows = self._request("GET", "api_keys", params={"select": "owner_id", "key_hash": f"eq.{key_hash}", "limit": "1"})
        return str(rows[0].get("owner_id") or "") if rows else ""

    def list_keys(self, key_hash: str = "") -> list[dict[str, Any]]:
        params = {"select": "key_hash,name,created", "order": "created.desc"}
        owner = self.key_owner(key_hash) if key_hash else ""
        if owner:
            params["owner_id"] = f"eq.{owner}"
        elif key_hash:
            params["key_hash"] = f"eq.{key_hash}"
        return [{"id": row["key_hash"], "name": row["name"], "created": row["created"]}
                for row in (self._request("GET", "api_keys", params=params) or [])]

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
        result = self._request("POST", "usage_log", data=row,
                               prefer="return=representation,resolution=ignore-duplicates")
        return bool(result)

    def _rows(self, key_hash: str) -> list[dict[str, Any]]:
        # ponytail: paginate in Python until account volumes justify one SQL aggregate RPC.
        rows: list[dict[str, Any]] = []
        offset = 0
        owner = self.key_owner(key_hash)
        while True:
            params = {"select": "*", "order": "ts.desc", "limit": "1000", "offset": str(offset)}
            if owner:
                params["or"] = f"(owner_id.eq.{owner},key_hash.eq.{key_hash})"
            else:
                params["key_hash"] = f"eq.{key_hash}"
            page = self._request("GET", "usage_log", params=params) or []
            rows.extend(page)
            if len(page) < 1000:
                return rows
            offset += 1000

    def _all_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._request("GET", "usage_log", params={"select": "*", "order": "ts.desc",
                                 "limit": "1000", "offset": str(offset)}) or []
            rows.extend(page)
            if len(page) < 1000:
                return rows
            offset += 1000

    def get_stats(self, key_hash: str) -> dict[str, Any]:
        rows = self._rows(key_hash)
        result = _stats(rows)
        result["by_pipeline"] = self.get_stats_by_pipeline(key_hash, rows=rows)
        result["by_agent"] = self.get_stats_by_agent(key_hash, rows=rows)
        return result

    def get_breakdown(self, key_hash: str) -> list[dict[str, Any]]:
        return _breakdown(self._rows(key_hash))

    def get_admin_stats(self) -> dict[str, Any]:
        return _stats(self._all_rows())

    def get_admin_breakdown(self) -> list[dict[str, Any]]:
        return _admin_breakdown(self._all_rows())

    def _legacy_group(self, key_hash: str, field: str, pipeline: str = "", rows=None) -> list:
        rows = rows if rows is not None else self._rows(key_hash)
        if pipeline:
            rows = [r for r in rows if r.get("pipeline") == pipeline]
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(row.get(field) or "")].append(row)
        result = []
        for label, items in groups.items():
            stat = _stats(items)
            result.append({field: label, "calls": len(items), "tokens_saved": stat["total_tokens_saved"],
                           "avg_savings_pct": stat["avg_savings_pct"], "avg_quality": stat["avg_quality_proxy"],
                           "cost_saved_usd": stat["total_verified_savings_usd"],
                           "brevitas_fee_usd": stat["total_brevitas_fee_usd"]})
        return result

    def get_stats_by_pipeline(self, key_hash: str, start: str = "", end: str = "", rows=None) -> list:
        return self._legacy_group(key_hash, "pipeline", rows=rows)

    def get_stats_by_agent(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "", rows=None) -> list:
        return self._legacy_group(key_hash, "agent", pipeline, rows)

    def get_stats_by_run(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "") -> list:
        return self._legacy_group(key_hash, "run_id", pipeline)

    def set_provider_config(self, key_hash: str, provider: str, provider_api_key: str, model: str) -> None:
        self._request("POST", "provider_config", data={"key_hash": key_hash, "provider": provider,
                      "provider_api_key": provider_api_key, "model": model}, prefer="resolution=merge-duplicates")

    def get_provider_config(self, key_hash: str) -> dict | None:
        rows = self._request("GET", "provider_config", params={"select": "provider,provider_api_key,model", "key_hash": f"eq.{key_hash}", "limit": "1"})
        return rows[0] if rows else None


def make_store():
    backend = os.getenv("BREVITAS_STORE", "").lower()
    configured = bool((os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL"))
                      and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    if backend == "supabase" or (backend != "sqlite" and configured):
        return SupabaseUsageStore()
    return UsageStore(os.getenv("BREVITAS_SQLITE_PATH") or None)
