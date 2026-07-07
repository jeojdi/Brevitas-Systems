import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Cost per 1M tokens (USD) — updated from provider pricing pages
PROVIDER_COSTS_PER_1M: dict = {
    "anthropic": {
        "claude-opus-4-8":           {"input": 15.00, "output": 75.00},
        "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
        "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    },
    "openai": {
        "gpt-4o":      {"input": 2.50,  "output": 10.00},
        "gpt-4o-mini": {"input": 0.15,  "output": 0.60},
        "o3-mini":     {"input": 1.10,  "output": 4.40},
    },
    "grok": {
        # xAI Grok (api.x.ai). Figures from public pricing trackers, mid-2026 —
        # confirm against docs.x.ai/developers/models before relying on the billed fee.
        "grok-4":       {"input": 3.00,  "output": 15.00},
        "grok-4-fast":  {"input": 0.20,  "output": 0.50},
        "grok-4.1-fast":{"input": 0.20,  "output": 0.50},
        "grok-3":       {"input": 2.00,  "output": 10.00},
        "grok-3-mini":  {"input": 0.30,  "output": 0.50},
    },
    "mistral": {
        # api.mistral.ai. Public-tracker figures, mid-2026 — confirm at mistral.ai/pricing.
        "mistral-large-latest": {"input": 2.00, "output": 6.00},
        "mistral-small-latest": {"input": 0.20, "output": 0.60},
        "codestral-latest":     {"input": 0.30, "output": 0.90},
    },
    "google": {
        # Gemini via Google's OpenAI-compat endpoint. Public-tracker figures, mid-2026 —
        # confirm at ai.google.dev/pricing. (This endpoint does not report cached_tokens,
        # so provider-cache savings aren't itemized here; our semantic cache still applies.)
        "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
        "gemini-2.5-pro":   {"input": 1.25, "output": 10.00},
        "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    },
    "deepseek": {
        "deepseek-chat":     {"input": 0.27, "output": 1.10},
        "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    },
    "ollama": {},
}


def infer_provider(model: str, given: str = "") -> str:
    """Best-effort provider from a model name.

    SDK/proxy callers may label every OpenAI-compatible call provider="openai"
    even when the upstream is DeepSeek/Groq, which breaks price lookup. The model
    name is authoritative, so prefer it; fall back to the caller-supplied provider.
    """
    m = (model or "").lower()
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith("grok") or m.startswith("groq"):
        return "grok"
    if m.startswith(("mistral", "magistral", "ministral", "codestral", "devstral", "pixtral")):
        return "mistral"
    if m.startswith(("gemini", "gemma")):
        return "google"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3"):
        return "openai"
    return given or ""


def cost_for_tokens(provider: str, model: str, tokens: int) -> float:
    """Return USD cost for `tokens` input tokens on a given provider/model."""
    rates = PROVIDER_COSTS_PER_1M.get(provider, {})
    rate = rates.get(model) or rates.get("default")
    if not rate:
        # Caller's provider label may be wrong for OpenAI-compatible upstreams;
        # retry under the provider inferred from the model name.
        inferred = infer_provider(model, provider)
        if inferred != provider:
            rate = PROVIDER_COSTS_PER_1M.get(inferred, {}).get(model)
    if not rate:
        return 0.0
    return tokens * rate["input"] / 1_000_000


class UsageStore:
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(__file__).parent / "brevitas.db")
        self.db_path = db_path
        self._init()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init(self) -> None:
        with self._conn() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_hash TEXT PRIMARY KEY,
                    name     TEXT NOT NULL,
                    created  TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS usage_log (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_hash         TEXT NOT NULL,
                    ts               TEXT NOT NULL,
                    baseline_tokens  INTEGER NOT NULL,
                    optimized_tokens INTEGER NOT NULL,
                    savings_pct      REAL NOT NULL,
                    quality_proxy    REAL NOT NULL,
                    provider         TEXT NOT NULL DEFAULT '',
                    model            TEXT NOT NULL DEFAULT '',
                    cost_saved_usd   REAL NOT NULL DEFAULT 0.0,
                    brevitas_fee_usd REAL NOT NULL DEFAULT 0.0,
                    session_id       TEXT NOT NULL DEFAULT '',
                    cached_tokens    INTEGER NOT NULL DEFAULT 0
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS provider_config (
                    key_hash         TEXT PRIMARY KEY,
                    provider         TEXT NOT NULL DEFAULT 'ollama',
                    provider_api_key TEXT NOT NULL DEFAULT '',
                    model            TEXT NOT NULL DEFAULT 'llama3.2'
                )
            """)
            # Migrate existing usage_log tables that lack the new columns
            existing = {r[1] for r in db.execute("PRAGMA table_info(usage_log)").fetchall()}
            for col, defn in [
                ("provider",         "TEXT NOT NULL DEFAULT ''"),
                ("model",            "TEXT NOT NULL DEFAULT ''"),
                ("cost_saved_usd",   "REAL NOT NULL DEFAULT 0.0"),
                ("brevitas_fee_usd", "REAL NOT NULL DEFAULT 0.0"),
                ("session_id",       "TEXT NOT NULL DEFAULT ''"),
                ("pipeline",         "TEXT NOT NULL DEFAULT ''"),
                ("agent",            "TEXT NOT NULL DEFAULT ''"),
                ("run_id",           "TEXT NOT NULL DEFAULT ''"),
                ("cached_tokens",    "INTEGER NOT NULL DEFAULT 0"),
            ]:
                if col not in existing:
                    db.execute(f"ALTER TABLE usage_log ADD COLUMN {col} {defn}")

    def create_key(self, key_hash: str, name: str) -> None:
        with self._conn() as db:
            db.execute(
                "INSERT OR IGNORE INTO api_keys VALUES (?, ?, ?)",
                (key_hash, name, datetime.now(timezone.utc).isoformat()),
            )

    def key_exists(self, key_hash: str) -> bool:
        with self._conn() as db:
            return db.execute(
                "SELECT 1 FROM api_keys WHERE key_hash = ?", (key_hash,)
            ).fetchone() is not None

    def list_keys(self) -> list:
        with self._conn() as db:
            rows = db.execute(
                "SELECT name, created FROM api_keys ORDER BY created DESC"
            ).fetchall()
        return [{"name": r[0], "created": r[1]} for r in rows]

    def record_usage(
        self,
        key_hash: str,
        baseline_tokens: int,
        optimized_tokens: int,
        savings_pct: float,
        quality_proxy: float,
        provider: str = "",
        model: str = "",
        cost_saved_usd: float = 0.0,
        brevitas_fee_usd: float = 0.0,
        session_id: str = "",
        pipeline: str = "",
        agent: str = "",
        run_id: str = "",
        cached_tokens: int = 0,
    ) -> None:
        with self._conn() as db:
            db.execute(
                "INSERT INTO usage_log "
                "(key_hash, ts, baseline_tokens, optimized_tokens, savings_pct, quality_proxy, "
                " provider, model, cost_saved_usd, brevitas_fee_usd, session_id, pipeline, agent, run_id, "
                " cached_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key_hash,
                    datetime.now(timezone.utc).isoformat(),
                    baseline_tokens,
                    optimized_tokens,
                    round(savings_pct, 4),
                    round(quality_proxy, 6),
                    provider,
                    model,
                    round(cost_saved_usd, 8),
                    round(brevitas_fee_usd, 8),
                    session_id,
                    pipeline,
                    agent,
                    run_id,
                    int(cached_tokens),
                ),
            )

    def set_provider_config(self, key_hash: str, provider: str, provider_api_key: str, model: str) -> None:
        with self._conn() as db:
            db.execute(
                """
                INSERT INTO provider_config (key_hash, provider, provider_api_key, model)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key_hash) DO UPDATE SET
                    provider         = excluded.provider,
                    provider_api_key = excluded.provider_api_key,
                    model            = excluded.model
                """,
                (key_hash, provider, provider_api_key, model),
            )

    def get_provider_config(self, key_hash: str) -> dict | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT provider, provider_api_key, model FROM provider_config WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
        if row is None:
            return None
        return {"provider": row[0], "provider_api_key": row[1], "model": row[2]}

    def get_stats(self, key_hash: str) -> dict:
        with self._conn() as db:
            agg = db.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(baseline_tokens - optimized_tokens), 0),
                    COALESCE(AVG(savings_pct), 0),
                    COALESCE(AVG(quality_proxy), 0),
                    COALESCE(SUM(baseline_tokens), 0),
                    COALESCE(SUM(optimized_tokens), 0),
                    COALESCE(SUM(cost_saved_usd), 0),
                    COALESCE(SUM(brevitas_fee_usd), 0)
                FROM usage_log WHERE key_hash = ?
                """,
                (key_hash,),
            ).fetchone()

            history = db.execute(
                """
                SELECT ts, baseline_tokens, optimized_tokens, savings_pct, quality_proxy,
                       provider, model, cost_saved_usd, brevitas_fee_usd
                FROM usage_log WHERE key_hash = ?
                ORDER BY ts DESC LIMIT 50
                """,
                (key_hash,),
            ).fetchall()

            billing = db.execute(
                """
                SELECT
                    strftime('%Y-%m', ts) as month,
                    COUNT(*) as calls,
                    COALESCE(SUM(baseline_tokens - optimized_tokens), 0) as tokens_saved,
                    COALESCE(SUM(cost_saved_usd), 0) as cost_saved_usd,
                    COALESCE(SUM(brevitas_fee_usd), 0) as brevitas_fee_usd
                FROM usage_log WHERE key_hash = ?
                GROUP BY month ORDER BY month DESC LIMIT 12
                """,
                (key_hash,),
            ).fetchall()

        calls, saved, avg_savings, avg_quality, total_base, total_opt, total_cost_saved, total_fee = agg

        # Get per-pipeline and per-agent breakdowns
        by_pipeline = self.get_stats_by_pipeline(key_hash)
        by_agent = self.get_stats_by_agent(key_hash)

        return {
            "total_calls": calls,
            "total_tokens_saved": saved,
            "avg_savings_pct": round(avg_savings, 2),
            "avg_quality_proxy": round(avg_quality, 4),
            "total_baseline_tokens": total_base,
            "total_optimized_tokens": total_opt,
            "total_cost_saved_usd": round(total_cost_saved, 6),
            "total_brevitas_fee_usd": round(total_fee, 6),
            "history": [
                {
                    "timestamp": h[0],
                    "baseline_tokens": h[1],
                    "optimized_tokens": h[2],
                    "savings_pct": h[3],
                    "quality_proxy": h[4],
                    "provider": h[5],
                    "model": h[6],
                    "cost_saved_usd": h[7],
                    "brevitas_fee_usd": h[8],
                }
                for h in history
            ],
            "billing_by_month": [
                {
                    "month": b[0],
                    "calls": b[1],
                    "tokens_saved": b[2],
                    "cost_saved_usd": round(b[3], 6),
                    "brevitas_fee_usd": round(b[4], 6),
                }
                for b in billing
            ],
            "by_pipeline": by_pipeline,
            "by_agent": by_agent,
        }

    def get_stats_by_pipeline(self, key_hash: str, start: str = "", end: str = "") -> list:
        """Get aggregated stats by pipeline."""
        with self._conn() as db:
            query = """
                SELECT
                    pipeline,
                    COUNT(*) as calls,
                    COALESCE(SUM(baseline_tokens - optimized_tokens), 0) as tokens_saved,
                    COALESCE(AVG(savings_pct), 0) as avg_savings_pct,
                    COALESCE(AVG(quality_proxy), 0) as avg_quality,
                    COALESCE(SUM(baseline_tokens), 0) as total_baseline,
                    COALESCE(SUM(optimized_tokens), 0) as total_optimized,
                    COALESCE(SUM(cost_saved_usd), 0) as cost_saved_usd,
                    COALESCE(SUM(brevitas_fee_usd), 0) as brevitas_fee_usd
                FROM usage_log
                WHERE key_hash = ?
            """
            params = [key_hash]

            if start:
                query += " AND ts >= ?"
                params.append(start)
            if end:
                query += " AND ts <= ?"
                params.append(end)

            query += " GROUP BY pipeline ORDER BY tokens_saved DESC"

            rows = db.execute(query, params).fetchall()

        return [
            {
                "pipeline": r[0] or "",
                "calls": r[1],
                "tokens_saved": r[2],
                "avg_savings_pct": round(r[3], 2),
                "avg_quality": round(r[4], 4),
                "cost_saved_usd": round(r[7], 6),
                "brevitas_fee_usd": round(r[8], 6),
            }
            for r in rows
        ]

    def get_stats_by_agent(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "") -> list:
        """Get aggregated stats by agent (optionally filtered by pipeline)."""
        with self._conn() as db:
            query = """
                SELECT
                    agent,
                    COUNT(*) as calls,
                    COALESCE(SUM(baseline_tokens - optimized_tokens), 0) as tokens_saved,
                    COALESCE(AVG(savings_pct), 0) as avg_savings_pct,
                    COALESCE(AVG(quality_proxy), 0) as avg_quality,
                    COALESCE(SUM(cost_saved_usd), 0) as cost_saved_usd,
                    COALESCE(SUM(brevitas_fee_usd), 0) as brevitas_fee_usd
                FROM usage_log
                WHERE key_hash = ?
            """
            params = [key_hash]

            if pipeline:
                query += " AND pipeline = ?"
                params.append(pipeline)
            if start:
                query += " AND ts >= ?"
                params.append(start)
            if end:
                query += " AND ts <= ?"
                params.append(end)

            query += " GROUP BY agent ORDER BY tokens_saved DESC"

            rows = db.execute(query, params).fetchall()

        return [
            {
                "agent": r[0] or "",
                "calls": r[1],
                "tokens_saved": r[2],
                "avg_savings_pct": round(r[3], 2),
                "avg_quality": round(r[4], 4),
                "cost_saved_usd": round(r[5], 6),
                "brevitas_fee_usd": round(r[6], 6),
            }
            for r in rows
        ]

    def get_stats_by_run(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "") -> list:
        """Get aggregated stats by run (optionally filtered by pipeline)."""
        with self._conn() as db:
            query = """
                SELECT
                    run_id,
                    COUNT(*) as calls,
                    COALESCE(SUM(baseline_tokens - optimized_tokens), 0) as tokens_saved,
                    COALESCE(AVG(savings_pct), 0) as avg_savings_pct,
                    COALESCE(AVG(quality_proxy), 0) as avg_quality,
                    COALESCE(SUM(cost_saved_usd), 0) as cost_saved_usd,
                    COALESCE(SUM(brevitas_fee_usd), 0) as brevitas_fee_usd
                FROM usage_log
                WHERE key_hash = ?
            """
            params = [key_hash]

            if pipeline:
                query += " AND pipeline = ?"
                params.append(pipeline)
            if start:
                query += " AND ts >= ?"
                params.append(start)
            if end:
                query += " AND ts <= ?"
                params.append(end)

            query += " GROUP BY run_id ORDER BY tokens_saved DESC"

            rows = db.execute(query, params).fetchall()

        return [
            {
                "run_id": r[0] or "",
                "calls": r[1],
                "tokens_saved": r[2],
                "avg_savings_pct": round(r[3], 2),
                "avg_quality": round(r[4], 4),
                "cost_saved_usd": round(r[5], 6),
                "brevitas_fee_usd": round(r[6], 6),
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Supabase-backed store — persists keys + usage so a backend redeploy does NOT
# wipe them (the SQLite store lives on Railway's ephemeral filesystem). Selected
# automatically when the Supabase service-role env vars are present; otherwise the
# SQLite UsageStore is used (local/dev). Same method surface as UsageStore.
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseUsageStore:
    """UsageStore backed by Supabase Postgres via the service-role client.

    Aggregations are done in Python (PostgREST has no GROUP BY), which is fine at
    dashboard volumes and keeps the code dependency-free beyond `supabase` (already
    used by api/mirror.py). Method signatures/return shapes match UsageStore exactly.
    """

    def __init__(self, url: str, service_key: str):
        from supabase import create_client
        self._c = create_client(url, service_key)

    # -- keys ---------------------------------------------------------------
    def create_key(self, key_hash: str, name: str) -> None:
        self._c.table("api_keys").upsert(
            {"key_hash": key_hash, "name": name, "created": _now_iso()},
            on_conflict="key_hash",
        ).execute()

    def key_exists(self, key_hash: str) -> bool:
        r = self._c.table("api_keys").select("key_hash").eq("key_hash", key_hash).limit(1).execute()
        return bool(r.data)

    def list_keys(self) -> list:
        r = self._c.table("api_keys").select("name, created").order("created", desc=True).execute()
        return [{"name": x["name"], "created": x["created"]} for x in (r.data or [])]

    # -- provider config ----------------------------------------------------
    def set_provider_config(self, key_hash: str, provider: str, provider_api_key: str, model: str) -> None:
        self._c.table("provider_config").upsert(
            {"key_hash": key_hash, "provider": provider,
             "provider_api_key": provider_api_key, "model": model},
            on_conflict="key_hash",
        ).execute()

    def get_provider_config(self, key_hash: str) -> dict | None:
        r = self._c.table("provider_config").select(
            "provider, provider_api_key, model").eq("key_hash", key_hash).limit(1).execute()
        if not r.data:
            return None
        row = r.data[0]
        return {"provider": row["provider"], "provider_api_key": row["provider_api_key"],
                "model": row["model"]}

    # -- usage --------------------------------------------------------------
    def record_usage(self, key_hash: str, baseline_tokens: int, optimized_tokens: int,
                     savings_pct: float, quality_proxy: float, provider: str = "",
                     model: str = "", cost_saved_usd: float = 0.0, brevitas_fee_usd: float = 0.0,
                     session_id: str = "", pipeline: str = "", agent: str = "", run_id: str = "",
                     cached_tokens: int = 0) -> None:
        self._c.table("usage_log").insert({
            "key_hash": key_hash, "ts": _now_iso(),
            "baseline_tokens": baseline_tokens, "optimized_tokens": optimized_tokens,
            "savings_pct": round(savings_pct, 4), "quality_proxy": round(quality_proxy, 6),
            "provider": provider, "model": model,
            "cost_saved_usd": round(cost_saved_usd, 8), "brevitas_fee_usd": round(brevitas_fee_usd, 8),
            "session_id": session_id, "pipeline": pipeline, "agent": agent, "run_id": run_id,
            "cached_tokens": int(cached_tokens),
        }).execute()

    def _rows(self, key_hash: str) -> list:
        r = self._c.table("usage_log").select("*").eq("key_hash", key_hash).execute()
        return r.data or []

    def get_stats(self, key_hash: str) -> dict:
        rows = self._rows(key_hash)
        n = len(rows)
        saved = sum(x["baseline_tokens"] - x["optimized_tokens"] for x in rows)
        base = sum(x["baseline_tokens"] for x in rows)
        opt = sum(x["optimized_tokens"] for x in rows)
        cost = sum(x.get("cost_saved_usd", 0) for x in rows)
        fee = sum(x.get("brevitas_fee_usd", 0) for x in rows)
        avg_sav = (sum(x["savings_pct"] for x in rows) / n) if n else 0
        avg_q = (sum(x.get("quality_proxy", 0) for x in rows) / n) if n else 0

        history = sorted(rows, key=lambda x: x["ts"], reverse=True)[:50]
        by_month: dict = {}
        for x in rows:
            m = (x["ts"] or "")[:7]
            b = by_month.setdefault(m, {"calls": 0, "tokens_saved": 0, "cost_saved_usd": 0.0,
                                        "brevitas_fee_usd": 0.0})
            b["calls"] += 1
            b["tokens_saved"] += x["baseline_tokens"] - x["optimized_tokens"]
            b["cost_saved_usd"] += x.get("cost_saved_usd", 0)
            b["brevitas_fee_usd"] += x.get("brevitas_fee_usd", 0)

        return {
            "total_calls": n,
            "total_tokens_saved": saved,
            "avg_savings_pct": round(avg_sav, 2),
            "avg_quality_proxy": round(avg_q, 4),
            "total_baseline_tokens": base,
            "total_optimized_tokens": opt,
            "total_cost_saved_usd": round(cost, 6),
            "total_brevitas_fee_usd": round(fee, 6),
            "history": [
                {"timestamp": h["ts"], "baseline_tokens": h["baseline_tokens"],
                 "optimized_tokens": h["optimized_tokens"], "savings_pct": h["savings_pct"],
                 "quality_proxy": h.get("quality_proxy", 0), "provider": h.get("provider", ""),
                 "model": h.get("model", ""), "cost_saved_usd": h.get("cost_saved_usd", 0),
                 "brevitas_fee_usd": h.get("brevitas_fee_usd", 0)}
                for h in history
            ],
            "billing_by_month": [
                {"month": m, "calls": v["calls"], "tokens_saved": v["tokens_saved"],
                 "cost_saved_usd": round(v["cost_saved_usd"], 6),
                 "brevitas_fee_usd": round(v["brevitas_fee_usd"], 6)}
                for m, v in sorted(by_month.items(), reverse=True)[:12]
            ],
            "by_pipeline": self.get_stats_by_pipeline(key_hash),
            "by_agent": self.get_stats_by_agent(key_hash),
        }

    def _group(self, rows: list, field: str) -> list:
        groups: dict = {}
        for x in rows:
            k = x.get(field) or ""
            g = groups.setdefault(k, {"calls": 0, "tokens_saved": 0, "sav": 0.0, "q": 0.0,
                                      "cost": 0.0, "fee": 0.0})
            g["calls"] += 1
            g["tokens_saved"] += x["baseline_tokens"] - x["optimized_tokens"]
            g["sav"] += x["savings_pct"]
            g["q"] += x.get("quality_proxy", 0)
            g["cost"] += x.get("cost_saved_usd", 0)
            g["fee"] += x.get("brevitas_fee_usd", 0)
        out = []
        for k, g in groups.items():
            c = g["calls"] or 1
            out.append({field: k, "calls": g["calls"], "tokens_saved": g["tokens_saved"],
                        "avg_savings_pct": round(g["sav"] / c, 2), "avg_quality": round(g["q"] / c, 4),
                        "cost_saved_usd": round(g["cost"], 6), "brevitas_fee_usd": round(g["fee"], 6)})
        return sorted(out, key=lambda r: r["tokens_saved"], reverse=True)

    def get_stats_by_pipeline(self, key_hash: str, start: str = "", end: str = "") -> list:
        return self._group(self._rows(key_hash), "pipeline")

    def get_stats_by_agent(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "") -> list:
        rows = [x for x in self._rows(key_hash) if not pipeline or x.get("pipeline") == pipeline]
        return self._group(rows, "agent")

    def get_stats_by_run(self, key_hash: str, pipeline: str = "", start: str = "", end: str = "") -> list:
        rows = [x for x in self._rows(key_hash) if not pipeline or x.get("pipeline") == pipeline]
        return self._group(rows, "run_id")


def make_store():
    """Return a persistent Supabase store when service-role creds are configured,
    else the local SQLite UsageStore. Falls back to SQLite if the client can't init."""
    url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        try:
            return SupabaseUsageStore(url, key)
        except Exception:
            pass
    return UsageStore()
