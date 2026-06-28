"""
Brevitas → Supabase mirror writer.

Forwards usage records with labels (pipeline, agent, run_id) from SQLite to Supabase
for analytics, billing, and cross-account aggregations.

Usage:
    from api.mirror import mirror_to_supabase

    # After recording usage in SQLite:
    mirror_to_supabase(
        user_id="uuid-string",
        key_hash="abc123",
        provider="openai",
        model="gpt-4",
        baseline_tokens=1000,
        optimized_tokens=500,
        session_id="sess_abc",
        pipeline="campaign-launch",
        agent="copywriter",
        run_id="run_xyz"
    )
"""
import os
import logging
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)


def mirror_to_supabase(
    user_id: str,
    key_hash: str,
    provider: str,
    model: str,
    baseline_tokens: int,
    optimized_tokens: int,
    session_id: str,
    pipeline: str = "",
    agent: str = "",
    run_id: str = "",
) -> bool:
    """
    Mirror a usage record to Supabase billing_events table with labels.

    Args:
        user_id: User UUID
        key_hash: API key hash
        provider: Provider name (openai, anthropic, deepseek)
        model: Model ID
        baseline_tokens: Tokens without Brevitas
        optimized_tokens: Tokens with Brevitas compression
        session_id: Session identifier
        pipeline: Pipeline name (optional)
        agent: Agent role (optional)
        run_id: Run/trace identifier (optional)

    Returns:
        True if successful, False if skipped or failed
    """
    # Skip if Supabase not configured
    supabase_url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url or not supabase_key:
        logger.debug("Supabase not configured; skipping mirror")
        return False

    try:
        from supabase import create_client

        client = create_client(supabase_url, supabase_key)

        # Calculate savings
        tokens_saved = baseline_tokens - optimized_tokens
        savings_pct = (
            (tokens_saved / baseline_tokens * 100) if baseline_tokens > 0 else 0
        )

        # Estimate cost based on provider rates
        cost_saved_usd = _estimate_cost_saved(provider, model, tokens_saved)

        # Prepare record with labels
        record = {
            "user_id": user_id,
            "key_hash": key_hash,
            "provider": provider,
            "model": model,
            "baseline_tokens": baseline_tokens,
            "optimized_tokens": optimized_tokens,
            "tokens_saved": tokens_saved,
            "savings_pct": round(savings_pct, 2),
            "cost_saved_usd": round(cost_saved_usd, 2),
            "session_id": session_id,
            "pipeline": pipeline or "",
            "agent": agent or "",
            "run_id": run_id or "",
            "created_at": datetime.utcnow().isoformat(),
        }

        # Insert into billing_events table
        result = client.table("billing_events").insert(record).execute()

        if result.data:
            logger.info(
                f"Mirrored usage to Supabase: {pipeline}/{agent}/{session_id} "
                f"({tokens_saved} tokens saved)"
            )
            return True
        else:
            logger.warning(f"Failed to mirror to Supabase: {result}")
            return False

    except ImportError:
        logger.debug("supabase package not installed; skipping mirror")
        return False
    except Exception as e:
        logger.error(f"Error mirroring to Supabase: {e}")
        return False


def _estimate_cost_saved(provider: str, model: str, tokens_saved: int) -> float:
    """
    Estimate cost saved based on provider token rates.

    Rates as of June 2026 (update as needed).
    """
    # Provider token pricing: (input_rate_per_1m, output_rate_per_1m)
    rates = {
        # OpenAI
        "openai": {
            "gpt-4-turbo": (0.01, 0.03),
            "gpt-4": (0.03, 0.06),
            "gpt-3.5-turbo": (0.0005, 0.0015),
        },
        # Anthropic
        "anthropic": {
            "claude-opus-4-8": (0.015, 0.075),
            "claude-sonnet-4-6": (0.003, 0.015),
            "claude-haiku-4-5": (0.0008, 0.004),
        },
        # DeepSeek
        "deepseek": {
            "deepseek-chat": (0.00014, 0.00028),
            "deepseek-reasoner": (0.00055, 0.0022),
        },
    }

    # Default: average 50% input, 50% output tokens
    provider_rates = rates.get(provider, {})
    model_rates = provider_rates.get(model, (0.001, 0.001))

    input_rate, output_rate = model_rates
    avg_rate = (input_rate + output_rate) / 2

    # Cost = tokens_saved × average_rate / 1M
    cost_saved = (tokens_saved * avg_rate) / 1_000_000

    return max(0.0001, cost_saved)  # Minimum $0.0001 per record


def batch_mirror_to_supabase(records: list[dict]) -> int:
    """
    Mirror multiple usage records to Supabase in a batch.

    Args:
        records: List of record dicts with keys:
            user_id, key_hash, provider, model, baseline_tokens,
            optimized_tokens, session_id, pipeline, agent, run_id

    Returns:
        Number of records successfully mirrored
    """
    supabase_url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url or not supabase_key:
        logger.debug("Supabase not configured; skipping batch mirror")
        return 0

    try:
        from supabase import create_client

        client = create_client(supabase_url, supabase_key)

        # Enrich records with calculated fields and labels
        enriched_records = []
        for record in records:
            baseline = record.get("baseline_tokens", 0)
            optimized = record.get("optimized_tokens", 0)
            tokens_saved = baseline - optimized

            enriched = {
                "user_id": record["user_id"],
                "key_hash": record["key_hash"],
                "provider": record["provider"],
                "model": record["model"],
                "baseline_tokens": baseline,
                "optimized_tokens": optimized,
                "tokens_saved": tokens_saved,
                "savings_pct": round((tokens_saved / baseline * 100) if baseline > 0 else 0, 2),
                "cost_saved_usd": round(
                    _estimate_cost_saved(
                        record["provider"],
                        record["model"],
                        tokens_saved
                    ),
                    2
                ),
                "session_id": record.get("session_id", ""),
                "pipeline": record.get("pipeline", ""),
                "agent": record.get("agent", ""),
                "run_id": record.get("run_id", ""),
                "created_at": datetime.utcnow().isoformat(),
            }
            enriched_records.append(enriched)

        # Batch insert
        if enriched_records:
            result = client.table("billing_events").insert(enriched_records).execute()
            count = len(result.data) if result.data else 0
            logger.info(f"Batch mirrored {count}/{len(enriched_records)} records to Supabase")
            return count

        return 0

    except ImportError:
        logger.debug("supabase package not installed; skipping batch mirror")
        return 0
    except Exception as e:
        logger.error(f"Error batch mirroring to Supabase: {e}")
        return 0


def sync_labels_to_supabase(
    user_id: str,
    session_id: str,
    pipeline: str = "",
    agent: str = "",
    run_id: str = "",
) -> bool:
    """
    Update existing billing_events records with labels.

    This is useful when labels are resolved after the initial record is inserted.

    Args:
        user_id: User UUID
        session_id: Session ID to match
        pipeline: Pipeline name to set
        agent: Agent name to set
        run_id: Run ID to set

    Returns:
        True if successful
    """
    supabase_url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url or not supabase_key:
        logger.debug("Supabase not configured; skipping label sync")
        return False

    try:
        from supabase import create_client

        client = create_client(supabase_url, supabase_key)

        # Update records where session_id matches
        update_data = {}
        if pipeline:
            update_data["pipeline"] = pipeline
        if agent:
            update_data["agent"] = agent
        if run_id:
            update_data["run_id"] = run_id

        if not update_data:
            logger.debug("No labels to sync")
            return False

        result = (
            client.table("billing_events")
            .update(update_data)
            .eq("user_id", user_id)
            .eq("session_id", session_id)
            .execute()
        )

        if result.data:
            logger.info(
                f"Updated {len(result.data)} records with labels: "
                f"pipeline={pipeline}, agent={agent}, run_id={run_id}"
            )
            return True
        else:
            logger.warning(f"No records updated: {result}")
            return False

    except ImportError:
        logger.debug("supabase package not installed; skipping label sync")
        return False
    except Exception as e:
        logger.error(f"Error syncing labels to Supabase: {e}")
        return False
