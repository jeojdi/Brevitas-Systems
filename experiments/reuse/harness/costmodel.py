"""Dollars, from metered tokens times recorded prices.

§8.3 of the brief: fetch current published prices at run start, record them in the run record,
and compute cost from metered tokens times recorded price. Cache writes cost more than plain
input and cache reads cost far less, so a token-only comparison can invert the sign of the
result - which the arm B tuning probe demonstrated concretely: two configurations whose total
billable input tokens differ by 0.2% differ by about 7.9x in dollars on the prefix portion.

Prices below were fetched live on 2026-08-17 from provider documentation and are recorded here so
the computation is reproducible. The sha256 of this table goes in the run record; if a price
changes mid-matrix the affected cells are marked suspect.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

PRICE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing.md"
PRICE_FETCHED = "2026-08-17"

# Per million tokens, USD.
#   input       plain uncached input
#   output      generated tokens
#   write_5m    cache write on the default 5-minute tier   (1.25x input)
#   write_1h    cache write on the extended 1-hour tier    (2.00x input)
#   read        cache read, both tiers                     (0.10x input)
PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {
        "input": 1.00, "output": 5.00, "write_5m": 1.25, "write_1h": 2.00, "read": 0.10,
        "min_cacheable_prefix_tokens": 4096,
    },
    "claude-sonnet-5": {
        "input": 2.00, "output": 10.00, "write_5m": 2.50, "write_1h": 4.00, "read": 0.20,
        "min_cacheable_prefix_tokens": 1024,
    },
    "claude-opus-5": {
        "input": 5.00, "output": 25.00, "write_5m": 6.25, "write_1h": 10.00, "read": 0.50,
        "min_cacheable_prefix_tokens": 512,
    },
}

# Persistent store cost. A cross-run reuse store has an ongoing cost and omitting it flatters
# arm C (false-positive item 10). Priced against commodity object storage plus a served index;
# amortised per run in `run_cost`.
STORAGE_USD_PER_GB_MONTH = 0.023  # S3 Standard, us-east-1
INDEX_USD_PER_GB_MONTH = 0.25  # a served key-value index, order-of-magnitude
STORAGE_SOURCE = "https://aws.amazon.com/s3/pricing/"


def price_sheet_hash() -> str:
    blob = json.dumps(
        {"prices": PRICES, "storage": [STORAGE_USD_PER_GB_MONTH, INDEX_USD_PER_GB_MONTH],
         "fetched": PRICE_FETCHED},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve(model_version: str) -> dict[str, float]:
    if model_version in PRICES:
        return PRICES[model_version]
    for key, value in PRICES.items():
        if model_version.startswith(key) or key.startswith(model_version):
            return value
    raise KeyError(f"no recorded price for model {model_version!r}; refusing to estimate")


@dataclass
class Cost:
    input_usd: float = 0.0
    output_usd: float = 0.0
    cache_write_usd: float = 0.0
    cache_read_usd: float = 0.0

    @property
    def total(self) -> float:
        return self.input_usd + self.output_usd + self.cache_write_usd + self.cache_read_usd

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(
            self.input_usd + other.input_usd,
            self.output_usd + other.output_usd,
            self.cache_write_usd + other.cache_write_usd,
            self.cache_read_usd + other.cache_read_usd,
        )

    def to_json(self) -> dict[str, float]:
        return {
            "input_usd": self.input_usd,
            "output_usd": self.output_usd,
            "cache_write_usd": self.cache_write_usd,
            "cache_read_usd": self.cache_read_usd,
            "total_usd": self.total,
        }


def op_cost(
    *,
    model_version: str,
    input_tokens: int,
    output_tokens: int,
    ephemeral_5m_tokens: int,
    ephemeral_1h_tokens: int,
    cache_read_tokens: int,
) -> Cost:
    """Cost of one metered model call. Every token class priced separately, from the recorded
    sheet, never from a tokenizer estimate."""
    p = resolve(model_version)
    million = 1_000_000.0
    return Cost(
        input_usd=input_tokens * p["input"] / million,
        output_usd=output_tokens * p["output"] / million,
        cache_write_usd=(
            ephemeral_5m_tokens * p["write_5m"] + ephemeral_1h_tokens * p["write_1h"]
        ) / million,
        cache_read_usd=cache_read_tokens * p["read"] / million,
    )


def storage_cost_per_run(store_bytes: int, runs_amortised_over: int, retention_days: int = 30) -> float:
    """Amortised storage cost attributable to one run.

    Charged against arm C only, since arms A and B keep no store.
    """
    gb = store_bytes / (1024**3)
    months = retention_days / 30.0
    total = gb * (STORAGE_USD_PER_GB_MONTH + INDEX_USD_PER_GB_MONTH) * months
    return total / max(1, runs_amortised_over)


# Deployment scale over which a staleness bound is maintained. The shadow sample needed to bound
# staleness is a fixed NUMBER of recomputations, so the sampled *fraction* depends entirely on how
# many hits it is spread across. Declared here as a pre-registered constant rather than taken from
# a single run's hit count, which would be a category error: a customer does not re-establish the
# bound from scratch on every run.
DEPLOYMENT_HITS_PER_PERIOD = 100_000
STALENESS_BOUND = 0.01
STALENESS_CONFIDENCE = 0.95


def shadow_rate_for_bound(
    staleness_bound: float = STALENESS_BOUND,
    confidence: float = STALENESS_CONFIDENCE,
    n_hits: int = DEPLOYMENT_HITS_PER_PERIOD,
) -> float:
    """Fraction of reuse hits that must be recomputed to bound the stale-hit rate.

    A reuse layer cannot know a stored result is still correct unless it sometimes recomputes and
    compares. If validating reuse requires recomputation in production, that cost is real and
    belongs in the business case (false-positive item 9).

    Rule of three: observing zero stale hits in m samples bounds the true rate below 3/m at ~95%
    confidence, so m = 3/bound recomputations are needed. Spread over `n_hits`, that is a sampled
    fraction of m/n_hits.

    `n_hits` must be the DEPLOYMENT hit count, not one run's. Passing a single run's hit count
    yields a rate near 1.0 for any small run, which says only that you cannot establish a 1% bound
    from six observations - true, but not a property of the idea under test.
    """
    if staleness_bound <= 0 or n_hits <= 0:
        return 1.0
    scale = 3.0 if confidence >= 0.95 else 2.3  # 2.3 ~ 90%
    return min(1.0, (scale / staleness_bound) / n_hits)


def arm_c_total(
    *,
    executed_cost: Cost,
    served_hits_cost_if_recomputed: float,
    shadow_rate: float,
    store_bytes: int,
    runs_amortised_over: int,
) -> dict[str, float]:
    """Arm C's honest cost.

    `headline_usd` is the figure used in the primary metric: executed operations, plus the shadow
    recomputation needed to bound staleness, plus amortised storage. `shadow_free_usd` is reported
    beside it as the optimistic variant, never as the headline.
    """
    shadow_usd = served_hits_cost_if_recomputed * shadow_rate
    storage_usd = storage_cost_per_run(store_bytes, runs_amortised_over)
    return {
        "executed_usd": executed_cost.total,
        "shadow_usd": shadow_usd,
        "storage_usd": storage_usd,
        "shadow_free_usd": executed_cost.total,
        "headline_usd": executed_cost.total + shadow_usd + storage_usd,
        "shadow_rate": shadow_rate,
    }
