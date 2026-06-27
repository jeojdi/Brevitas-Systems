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
        "grok-3":      {"input": 3.00,  "output": 15.00},
        "grok-3-mini": {"input": 0.30,  "output": 0.50},
    },
    "deepseek": {
        "deepseek-chat":     {"input": 0.27, "output": 1.10},
        "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    },
    "ollama": {},
}


def cost_for_tokens(provider: str, model: str, tokens: int) -> float:
    """Return USD cost for `tokens` input tokens on a given provider/model."""
    rates = PROVIDER_COSTS_PER_1M.get(provider, {})
    rate = rates.get(model) or rates.get("default")
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
                    session_id       TEXT NOT NULL DEFAULT ''
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
    ) -> None:
        with self._conn() as db:
            db.execute(
                "INSERT INTO usage_log "
                "(key_hash, ts, baseline_tokens, optimized_tokens, savings_pct, quality_proxy, "
                " provider, model, cost_saved_usd, brevitas_fee_usd, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        }
