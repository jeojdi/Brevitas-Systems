"""
Capability registry for the Brevitas optimization audit.

Single source of truth for the 10 optimizations the audit checks. Consumed by the
static scanner (brevitas/audit.py), the /v1/audit endpoint, and the dashboard.

Each capability dict (shared contract):
    id              snake_case identifier
    name            human-readable name
    description     one sales-readable sentence
    providers       list of provider ids (scanner.broad vocabulary) or "all"
    detect          "static" | "traffic" | "both"
    weight          int 1-10, relative $ impact
    static_signals  list of {kind: "regex"|"import"|"call", pattern, meaning}
    traffic_metric  str metric id or None

Contract extensions used by the static scanner (optional keys):
    polarity               "positive" (default: signal => already_implemented) or
                           "negative" (signal is a VIOLATION => opportunity; e.g.
                           byte-stability, where we detect instability, not the fix)
    applicability_signals  signals that must match for the capability to be
                           applicable at all (e.g. streaming, concurrent fan-out);
                           none matched => not_applicable
    applicability_meaning  human sentence used when applicability_signals miss
    Per-signal extras: scope ("any" | "llm_file" — only count matches in files that
    also contain an LLM call signal), exclude (regex; skip matching lines, used to
    ignore logging lines), strength ("strong" default | "weak" — if ONLY weak
    signals match, verdict is "partial" instead of "already_implemented").

Verdict enum per capability:
    "already_implemented" | "partial" | "opportunity" | "not_applicable" | "unknown"

Honesty rule (the product): prefer already_implemented / not_applicable / unknown
over opportunity whenever the evidence is uncertain. A false "opportunity" a sales
engineer repeats and the prospect refutes kills the deal.
"""
from __future__ import annotations

_VOLATILE = (
    r"(?:datetime\.(?:now|utcnow)|time\.time\(|uuid\.uuid|uuid4\(|Date\.now\("
    r"|new\s+Date\(|Math\.random\(|random\.(?:random|randint|choice)\("
    r"|toISOString\(|strftime\()"
)
# Leading lookbehind keeps identifier substrings from matching: "filesystem",
# "ecosystem", "subsystem" must not read as prompt content. "_" and "." are NOT
# blocked so `system_prompt` / `msg.system` stay detectable; `os.system(` is
# handled by the per-signal exclude instead.
_PROMPTISH = r"(?<![A-Za-z0-9])(?:system|prompt|instruction|context|messages)"
# Lines that are observability plumbing, not prompt construction: volatile
# values here never reach provider cache keys, so flagging them fabricates a
# billing violation.
_NON_PROMPT_LINE = (
    r"\b(?:logg(?:er|ing)|console\.|print\(|debug|metric|trace_id"
    r"|telemetry|analytics|\bspan\b|posthog|sentry|os\.system\()"
)

CAPABILITIES = [
    {
        "id": "anthropic_cache_control",
        "name": "Anthropic prompt caching (cache_control)",
        "description": "Marks stable prompt prefixes with cache_control so Anthropic bills cached reads at a fraction of fresh input.",
        "providers": ["anthropic", "bedrock"],
        "detect": "both",
        "weight": 9,
        "static_signals": [
            {"kind": "regex", "pattern": r"cache_control", "meaning": "explicit cache_control marker on prompt content"},
            {"kind": "regex", "pattern": r"type[\"']?\s*[:=]\s*[\"']ephemeral", "meaning": "ephemeral cache block on message content", "scope": "llm_file"},
            # Bedrock's Converse API spells the same optimization differently:
            # {"cachePoint": {"type": "default"}} blocks. A boto3 repo already
            # placing cachePoint markers is fully cached — without this signal
            # it would be reported as the worst-case opportunity.
            {"kind": "regex", "pattern": r"cachePoint|cache_point", "meaning": "Bedrock Converse cachePoint marker on prompt content"},
        ],
        "traffic_metric": "caller_cache_markers",
    },
    {
        "id": "openai_prompt_cache_key",
        "name": "OpenAI prompt_cache_key routing",
        "description": "Pins repeat prompts to the same OpenAI cache shard with prompt_cache_key, raising automatic-cache hit rates.",
        "providers": ["openai", "azure_openai"],
        "detect": "static",
        "weight": 6,
        "static_signals": [
            {"kind": "regex", "pattern": r"prompt_cache_key|promptCacheKey", "meaning": "prompt_cache_key passed on completion requests"},
        ],
        "traffic_metric": None,
    },
    {
        "id": "prompt_byte_stability",
        "name": "Prompt byte-stability",
        "description": "Keeps system/early prompt bytes identical across calls (no timestamps, UUIDs, or unstable serialization) so provider caches can hit.",
        "providers": "all",
        "detect": "both",
        "weight": 8,
        "polarity": "negative",
        "static_signals": [
            {
                "kind": "regex",
                "pattern": _PROMPTISH + r"[^\n]{0,120}" + _VOLATILE,
                "meaning": "volatile value (timestamp/uuid/random) interpolated into prompt content",
                "scope": "llm_file",
                "exclude": _NON_PROMPT_LINE,
            },
            {
                "kind": "regex",
                # Strict word boundary on both sides: json.dumps(context_snapshot)
                # is telemetry, not prompt content — only a bare promptish
                # identifier counts. Underdetection beats a refutable claim.
                "pattern": r"json\.dumps\((?![^)\n]*sort_keys)[^)\n]*?\b(?:context|messages|prompt)\b",
                "meaning": "json.dumps without sort_keys feeding prompt content — key order may vary across builders",
                "scope": "llm_file",
                "exclude": _NON_PROMPT_LINE,
            },
        ],
        "traffic_metric": "cache_hit_ratio",
    },
    {
        "id": "xai_conv_affinity",
        "name": "xAI conversation affinity (x-grok-conv-id)",
        "description": "Sends the x-grok-conv-id header on Grok requests — without it xAI's prompt cache never hits (verified live 2026-07-28).",
        "providers": ["xai"],
        "detect": "both",
        "weight": 7,
        "static_signals": [
            {"kind": "regex", "pattern": r"x-grok-conv-id", "meaning": "x-grok-conv-id affinity header set on xAI requests"},
        ],
        "traffic_metric": "xai_cache_hits",
    },
    {
        "id": "stream_usage_receipts",
        "name": "Streaming usage receipts (stream_options.include_usage)",
        "description": "Requests token usage on streamed responses so every streamed call still produces an exact billing receipt.",
        "providers": ["openai", "azure_openai", "xai", "deepseek", "groq"],
        "detect": "both",
        "weight": 4,
        "applicability_signals": [
            {"kind": "regex", "pattern": r"stream\s*[:=]\s*[Tt]rue|[\"']stream[\"']\s*:\s*true|\.stream\(", "meaning": "streams LLM responses", "scope": "llm_file"},
        ],
        "applicability_meaning": "no streamed LLM calls detected",
        "static_signals": [
            {"kind": "regex", "pattern": r"include_usage", "meaning": "include_usage requested for streamed responses"},
            {"kind": "regex", "pattern": r"stream_options", "meaning": "stream_options configured on streamed calls"},
        ],
        "traffic_metric": "stream_receipt_visibility",
    },
    {
        "id": "exact_repeat_cache",
        "name": "Exact-repeat response caching",
        "description": "Serves byte-identical repeat requests from a local cache instead of paying the provider again.",
        "providers": "all",
        "detect": "both",
        "weight": 7,
        "static_signals": [
            {"kind": "regex", "pattern": r"lru_cache|functools\.cache\b|cachetools|memoiz", "meaning": "memoization wrapped around LLM call helpers", "scope": "llm_file"},
            {
                "kind": "regex",
                "pattern": r"(?:redis|keyv|node-cache)[^\n]{0,120}(?:completion|chat|llm|response)|(?:completion|chat|llm)[^\n]{0,120}(?:cache\.get|cache\.set|redis)",
                "meaning": "external cache read/write around LLM responses",
                "scope": "llm_file",
                "strength": "weak",
            },
        ],
        "traffic_metric": "exact_repeat_rate",
    },
    {
        "id": "predictive_cache_warming",
        "name": "Predictive cache warming",
        "description": "Pings provider caches ahead of predicted traffic so expiring prefixes stay warm instead of re-billing as fresh writes.",
        # Must mirror brevitas.warming.provider_warmable(): warming pings can
        # only pay for themselves on these providers. groq/fireworks cached
        # reads bill 0.50x (a ping costs what a hit saves), perplexity has no
        # cached-input discount, mistral/xai/together/openrouter have
        # undocumented TTLs. Claiming warming as an "opportunity" there is a
        # prospect-refutable false claim — keep them not_applicable.
        "providers": ["anthropic", "openai", "deepseek"],
        "detect": "both",
        "weight": 5,
        "static_signals": [
            {
                "kind": "regex",
                "pattern": r"cache[_\- ]?warm|warm[_\- ]?(?:cache|prefix|ping)|pre[_\- ]?warm|keep[_\- ]?alive[^\n]{0,40}(?:cache|prompt|prefix)",
                "meaning": "keep-alive / pre-warm pinging of provider caches",
            },
        ],
        "traffic_metric": "warming_status",
    },
    {
        "id": "cache_ttl_tiering",
        "name": "Cache TTL tiering (5m vs 1h)",
        "description": "Chooses the 1-hour cache tier for slow-repeat prefixes where the default 5-minute TTL expires between calls.",
        "providers": ["anthropic", "bedrock"],
        "detect": "both",
        "weight": 3,
        "applicability_signals": [
            {"kind": "regex", "pattern": r"cache_control", "meaning": "uses cache_control (TTL tiering builds on it)"},
        ],
        "applicability_meaning": "no cache_control usage detected, so TTL tiering is covered under prompt caching itself",
        "static_signals": [
            {"kind": "regex", "pattern": r"ttl[\"']?\s*[:=]\s*[\"']?(?:1h|5m)", "meaning": "explicit ttl on cache_control blocks"},
            {"kind": "regex", "pattern": r"cache_control[^\n]{0,120}ttl", "meaning": "ttl configured alongside cache_control"},
        ],
        "traffic_metric": "ttl_tier_writes",
    },
    {
        "id": "usage_metering",
        "name": "Usage & cost metering",
        "description": "Logs per-call token usage so spend is attributable and optimizations are measurable.",
        "providers": "all",
        "detect": "static",
        "weight": 4,
        "static_signals": [
            {
                "kind": "regex",
                "pattern": r"prompt_tokens|completion_tokens|input_tokens|output_tokens|total_tokens|cached_tokens|cache_read_input_tokens",
                "meaning": "reads token-usage fields from provider responses",
                "scope": "llm_file",
            },
            {"kind": "regex", "pattern": r"tiktoken|count_tokens|get_encoding\(", "meaning": "counts tokens locally", "scope": "llm_file", "strength": "weak"},
            {"kind": "regex", "pattern": r"langfuse|helicone|promptlayer|portkey|braintrust|openmeter|weave\.init", "meaning": "LLM observability/metering vendor integrated"},
        ],
        "traffic_metric": None,
    },
    {
        "id": "concurrent_prefix_batching",
        "name": "Concurrent same-prefix coordination",
        "description": "Coordinates concurrent fan-out calls sharing a prompt prefix so one request writes the cache before the rest fire.",
        "providers": "all",
        "detect": "static",
        "weight": 5,
        "applicability_signals": [
            {
                "kind": "regex",
                "pattern": r"asyncio\.gather|Promise\.all(?:Settled)?\(|ThreadPoolExecutor|concurrent\.futures|p-limit|pLimit\(",
                "meaning": "fans out concurrent LLM calls",
                "scope": "llm_file",
            },
        ],
        "applicability_meaning": "no concurrent LLM fan-out detected",
        "static_signals": [
            {"kind": "regex", "pattern": r"singleflight|single_flight|coalesc|in[_\- ]?flight[^\n]{0,40}(?:dedup|map|lock)|request[_\- ]?dedup", "meaning": "in-flight request coordination / deduplication"},
        ],
        "traffic_metric": None,
    },
]

VERDICTS = ("already_implemented", "partial", "opportunity", "not_applicable", "unknown")
