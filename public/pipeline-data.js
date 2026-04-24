// Brevitas — hardcoded pipeline outputs from real Claude calls.
// Four coding tasks, three agents each (Architect → Builder → Reviewer).
// Token classification (kept / redundant / filler / structural) is computed
// at data-load time so the UI can style them without re-running the model.

window.BrevitasPipelineData = (() => {

  // Raw pipeline outputs (captured from live claude.complete runs).
  const RAW = {
    'rate-limiter': {
      label: 'Rate limiter',
      user: 'Add token-bucket rate limiting to the API gateway, 100 req/min per user, Redis-backed.',
      a1: "Implement token-bucket rate limiting using Redis as the backing store with a capacity of 100 tokens per user per minute. Use the GCRA (Generic Cell Rate Algorithm) implemented via Lua scripts in Redis to ensure atomic increment-and-check operations, eliminating race conditions. Store bucket state in Redis hash keys keyed by user:{userId}:bucket with fields tracking tokens and last_refill_timestamp. Configure the refill rate at 100/60 = 1.667 tokens per second. On each request, execute the Lua script to atomically decrement the bucket; reject with 429 status if tokens are exhausted. Set Redis key TTL to 120 seconds to auto-cleanup inactive users. Wire the rate-limit middleware into the API gateway before routing to prevent downstream overload.",
      a2: "Implement token-bucket rate limiting via Redis with atomic GCRA operations to enforce 100 requests per minute per user. Create a RateLimiter class wrapping a Redis client, exposing an allowRequest(userId) method that executes a Lua script atomically checking and decrementing tokens stored in user:{userId}:bucket hash keys with tokens and lastRefillTimestamp fields. The script calculates refill amount based on elapsed time at 1.667 tokens/second, updates bucket state, and returns a boolean indicating whether the request is permitted. Configure Redis key expiration to 120 seconds for auto-cleanup. Integrate RateLimitMiddleware into the API gateway request pipeline before route handlers, extracting userId from request context and invoking allowRequest, returning 429 status with Retry-After header on denial. Wire the middleware as early in the chain as possible to prevent downstream resource consumption.",
      a3: "The prior two hops designed and implemented token-bucket rate limiting using Redis-backed GCRA with atomic Lua scripts to enforce 100 requests per minute per user, integrated as middleware in the API gateway pipeline. First, the Lua script lacks explicit handling for clock skew or Redis server time drift — if the server clock shifts backward, lastRefillTimestamp comparisons will produce incorrect refill calculations, potentially granting excess tokens. Add server-side clock validation or use Redis's monotonic time source to ensure timestamps only move forward. Second, the 120-second TTL is too aggressive; under normal usage patterns at 100 req/min, a single user inactive for two minutes loses all bucket state, forcing a cold restart that grants a sudden token burst on next request. Increase TTL to at least 3600 seconds or tie expiration to actual usage patterns. Third, the Retry-After header is missing the calculated backoff value — it should return Retry-After: <seconds-until-refill> computed from the script response, not just a static value. Approve with revisions.",
    },
    'cache-invalidation': {
      label: 'Cache invalidation',
      user: 'Product catalog cache keeps serving stale prices after updates. Fix it.',
      a1: "Implement a write-through cache invalidation pattern using Redis with 5-minute TTL for price keys, triggering immediate purge on product updates via Kafka events. Store prices in a two-tier structure: hot tier (Redis, sub-second latency) backed by PostgreSQL with row-level versioning. Add event-driven invalidation: publish ProductPriceUpdated events to Kafka topic with partition key on product_id, consumed by cache service executing DEL commands within 100ms. Introduce cache versioning headers (ETag/Last-Modified) for client-side validation. Monitor cache hit ratio threshold of 85%; below that triggers diagnostic logging of invalidation lag.",
      a2: "Implement write-through cache invalidation using Redis with 5-minute TTL and event-driven purging via Kafka to eliminate stale prices. The PriceCache class manages the two-tier structure: RedisClient handles hot-tier operations with sub-second GET/SET on keys like price:product:{productId}:v{version}, while PostgreSQL stores canonical prices with version columns. ProductPriceUpdated events published to Kafka topic products.prices.updated partition by product_id trigger CacheInvalidationConsumer to execute DEL commands within 100ms. The PriceService decorates responses with ETag and Last-Modified headers for client validation. CacheMetricsCollector tracks hit_ratio metric, logging detailed invalidation_lag diagnostics when ratio drops below 85% threshold.",
      a3: "The architect and builder propose a two-tier Redis-PostgreSQL cache with Kafka-driven invalidation to eliminate stale prices. First, the 5-minute TTL creates a hard staleness window — if Kafka delivery delays exceed 100ms or the consumer crashes, prices remain stale for minutes; add a secondary TTL-based refresh trigger at 4 minutes post-update and circuit-breaker logic to fall through to PostgreSQL on invalidation failure. Second, partitioning by product_id alone doesn't prevent thundering herd on hot products with many concurrent requests post-invalidation; add a request coalescing layer (single-flight caching) to batch cache-fill queries. Third, the 85% hit-ratio threshold is reactive; proactively monitor invalidation_lag percentiles (p99) and alert when approaching the 100ms SLA rather than waiting for cache effectiveness to degrade. Approve with revisions pending these safeguards.",
    },
    'feature-flag': {
      label: 'Feature flags',
      user: 'Build a feature-flag service supporting % rollout, user targeting, and instant kill-switch.',
      a1: "Use a stateless Node.js/Express API backed by Redis for flag state (sub-10ms reads) and PostgreSQL for audit logs and targeting rules. Store flag definitions as JSON documents with rollout percentage, user ID/cohort predicates, and boolean overrides. Implement consistent hashing (MurmurHash3) on user ID to assign users to percentage buckets deterministically. Deploy behind a CDN with 60-second cache headers for read-heavy workloads. Use pub/sub (Redis streams or PostgreSQL LISTEN) to propagate kill-switch changes to all SDK clients within 500ms. SDKs evaluate flags locally by downloading the ruleset every 5 minutes or on push events, reducing latency to single-digit milliseconds on the client side. Support atomic flag mutations with version vectors to prevent race conditions during rollouts.",
      a2: "The stateless API uses Redis for sub-10ms flag reads and PostgreSQL for audit logs, with SDKs evaluating flags locally via periodic ruleset downloads and push invalidation. The FlagService class queries Redis cache for FlagDefinition documents containing rolloutPercentage, userPredicates array, and override booleans; cache misses fetch from PostgreSQL and store with 60-second TTL. The EvaluationEngine applies MurmurHash3 hashing on userId to deterministically map users into percentage buckets, then evaluates user predicates against targeting rules. Kill-switch changes publish via Redis pub/sub with 500ms propagation SLA to connected clients. The SDK polls flagRulesetUrl every 300 seconds and listens for invalidation events, storing FlagRuleset locally to enable sub-millisecond client-side evaluation. Mutations use versionVector fields to detect concurrent updates atomically, with FlagMutationHandler comparing versions before persisting to PostgreSQL and invalidating cache keys via Redis DEL.",
      a3: "The prior two hops designed a stateless Node.js/Express feature-flag service using Redis for fast reads, PostgreSQL for audit trails, and local SDK evaluation with deterministic user hashing, plus a built implementation with cache layers, pub/sub kill-switches, and version vectors for atomic mutations. First, the 60-second CDN cache on flag reads creates a dangerous gap for kill-switches: even with 500ms pub/sub propagation, CDN-cached responses may serve stale flags for a full minute post-incident. Instead, implement Cache-Control: max-age=5, must-revalidate and a Surrogate-Key header so the kill-switch handler purges the CDN edge immediately via API calls to Cloudflare or Fastly. Second, the 5-minute SDK polling interval risks flag changes going unnoticed for 300 seconds if push events fail silently. Add exponential backoff with jitter when invalidation events don't arrive within a 10-second window, and include a lastSeenVersion header in poll requests so the server can force a re-download on mismatch. Third, MurmurHash3 consistency on user ID alone doesn't handle user cohort reassignment. Extend the hash input to include userId + cohortId + flagId and document the immutability requirement. Approve with revisions.",
    },
    'observability': {
      label: 'Tracing ingest',
      user: 'Ingest OpenTelemetry traces at 50k spans/sec, store 7-day window, query by trace_id.',
      a1: "Use a time-series database optimized for sparse, high-cardinality data: Jaeger with Elasticsearch backend or Grafana Tempo with S3/GCS. Ingest via OTLP gRPC collector, batching spans at 512-byte threshold to achieve 50k/sec throughput. Partition trace storage by day with TTL=7 days; use trace_id as primary shard key to enable sub-millisecond lookups. Index trace_id and service tags only — avoid full-text indexing. For 50k spans/sec over 7 days expect ~30TB uncompressed; compress at rest to 3-5TB. Implement circuit-breaker on ingestion pipeline with queue depth ceiling at 10k pending batches to prevent memory exhaustion.",
      a2: "Use Grafana Tempo with S3 backend and OTLP gRPC ingestion to handle 50k spans/sec with sub-millisecond trace_id lookups. The OTLPReceiver service batches incoming spans using SpanBatcher with a 512-byte threshold, flushing to TraceWriter which partitions data by day and stores compressed blocks in S3 with automatic 7-day TTL expiration. Implement CircuitBreaker in the ingestion pipeline with maxQueueDepth of 10k pending batches; when exceeded, reject writes with HTTP 429 to prevent memory exhaustion. The TraceIndex maintains an in-memory map of trace_id to S3 block locations, updated on each write. QueryEngine retrieves trace_id matches directly from TraceIndex, avoiding full-text search, and deserializes compressed blocks from S3 on demand. Expect ~3-5TB compressed storage across 7 days from the baseline 30TB uncompressed volume.",
      a3: "The prior two hops converged on Grafana Tempo with S3 storage, OTLP gRPC batching at 512 bytes, and daily partitioning with 7-day TTL to handle 50k spans/sec with trace_id-indexed queries. First, the in-memory TraceIndex mapping all trace_ids to S3 block locations will exhaust memory under sustained load — at 50k spans/sec over 7 days, this index alone could grow to hundreds of GB; replace with a two-tier approach using a bounded LRU cache (10M entries, ~20GB) backed by a separate IndexStore such as DynamoDB or RocksDB on EBS for full lookups. Second, batching only at 512 bytes risks buffer stalls if individual spans exceed that threshold; set the threshold to 512KB with a 100ms flush timeout to avoid head-of-line blocking. Third, HTTP 429 backpressure alone may not prevent upstream client queue exhaustion; add explicit consumer lag monitoring and auto-scaling rules triggering pod replicas when queue depth exceeds 5k. Approve with revisions.",
    },
  };

  // --- Classifier ---
  // Connectives / filler we strike through in optimized view.
  const FILLER = new Set([
    'the','a','an','and','or','but','of','to','in','on','at','for','with','by','from',
    'that','this','these','those','as','is','are','was','were','be','been','being',
    'it','its','it\'s','will','would','should','could','can','may','might','must',
    'should','has','have','had','having','do','does','did','not','no','nor',
    'also','very','just','only','even','such','any','each','every','some','most','many','much','more','less','least',
    'than','so','if','then','while','when','where','which','who','whom','whose',
    'into','onto','over','under','across','between','through','during','before','after','above','below',
    'via','per','one','two','three','four','five','six','seven','eight','nine','ten',
  ]);

  // Words highly associated with restating previous context.
  const REDUNDANT_MARKERS = [
    'prior two hops','the architect','the builder','the prior','the architect and builder',
    'architect recommended','builder implemented','builder proposes','the architect already',
    'previously','as stated','as noted','to recap','the previous',
  ];

  // Code-y identifier: has capital letter, underscore, dot, or paren followed by a word boundary
  const STRUCT_RE = /^[A-Z][A-Za-z0-9]*(?:[A-Za-z0-9_.()]+)?$|^[a-z]+[_.][a-z0-9_.()]+|^[a-z]+[A-Z][A-Za-z0-9]+|\(.*\)$/;

  function tokenize(str) {
    // Keep punctuation attached to the token; split on whitespace.
    return str.trim().split(/\s+/).map(t => ({ raw: t, lower: t.toLowerCase().replace(/[^a-z0-9]+/g, '') }));
  }

  // Classify a single message's tokens.
  // Redundant regions are detected by finding any of the REDUNDANT_MARKERS inside the text
  // and marking tokens in that sentence (ends at . or ;) as redundant.
  function classify(text) {
    // First pass: find redundant sentences.
    const sentences = text.split(/(?<=[.!?])\s+/);
    const redundantSentIdx = new Set();
    sentences.forEach((s, i) => {
      const low = s.toLowerCase();
      if (REDUNDANT_MARKERS.some(m => low.includes(m))) redundantSentIdx.add(i);
    });

    // Second pass: tokenize, assign kind per token.
    const out = [];
    sentences.forEach((s, si) => {
      const toks = tokenize(s);
      toks.forEach((t, ti) => {
        let k = 'filler';
        const cleaned = t.raw.replace(/[.,;:!?)\]]+$/, '').replace(/^[([{]+/, '');
        if (redundantSentIdx.has(si)) {
          k = 'redundant';
        } else if (STRUCT_RE.test(cleaned) && cleaned.length > 2) {
          k = 'structural';
        } else if (!FILLER.has(t.lower) && t.lower.length > 2) {
          k = 'kept';
        }
        out.push({ t: t.raw, k, si, ti });
      });
      // End-of-sentence marker (used for line breaks maybe)
      if (si < sentences.length - 1) out.push({ t: ' ', k: 'space', si, ti: -1 });
    });
    return out;
  }

  // Rough token count heuristic — chars/4.
  const countTokens = (text) => Math.ceil(text.length / 4);

  const TASKS = Object.entries(RAW).map(([id, r]) => {
    const a1 = classify(r.a1);
    const a2 = classify(r.a2);
    const a3 = classify(r.a3);
    const a1Tokens = countTokens(r.a1);
    const a2Tokens = countTokens(r.a2);
    const a3Tokens = countTokens(r.a3);
    const uTk = countTokens(r.user) + 180; // + system prompt overhead

    // Baseline per-hop input: user prompt + all prior outputs (each hop re-sends full history)
    const baseline = {
      call1: uTk,
      call2: uTk + a1Tokens,
      call3: uTk + a1Tokens + a2Tokens,
    };
    baseline.total = baseline.call1 + baseline.call2 + baseline.call3;

    // Optimized per-hop: references + compression on restated portions
    // First call untouched. Call 2: restated a1 -> reference (90% saved on restated portion),
    // plus 30% compression on remainder. Call 3 same idea on prior.
    const optimized = {
      call1: baseline.call1,
      call2: Math.round(uTk * 0.95 + a1Tokens * 0.15), // a1 becomes a reference
      call3: Math.round(uTk * 0.95 + a1Tokens * 0.08 + a2Tokens * 0.18), // deeper compression
    };
    optimized.total = optimized.call1 + optimized.call2 + optimized.call3;
    const pctSaved = Math.round((1 - optimized.total / baseline.total) * 100);

    return {
      id, label: r.label, user: r.user,
      a1, a2, a3,
      a1Raw: r.a1, a2Raw: r.a2, a3Raw: r.a3,
      a1Tokens, a2Tokens, a3Tokens,
      baseline, optimized, pctSaved,
    };
  });

  const dropReason = {
    filler: 'Connective tissue — carries no task-relevant meaning.',
    redundant: 'Restated from a previous hop — replaced with a shared-memory reference.',
    kept: 'Meaning-bearing token — preserved.',
    structural: 'Code identifier — preserved verbatim.',
    space: '',
  };

  return { TASKS, dropReason };
})();
