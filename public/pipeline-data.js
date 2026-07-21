// Brevitas — hardcoded pipeline outputs from real Claude calls.
// Four coding tasks, three agents each (Architect → Builder → Reviewer).
// The demo models Brevitas's default content-preserving path: provider-native prefix caching.
// Agent outputs remain intact; only the price paid for repeated input context changes.

window.BrevitasPipelineData = (() => {

  // Raw pipeline outputs (captured from live claude.complete runs).
  const RAW = {
    'rate-limiter': {
      label: 'Rate limiter',
      user: 'Add token-bucket rate limiting to the API gateway, 100 req/min per user, Redis-backed.',
      a1: "Give each user an allowance of 100 requests per minute, and top it back up gradually so it refills smoothly instead of resetting all at once. Keep track of how many requests each person has left in Redis, a fast shared memory store, so every server sees the same count. Check and update that count in a single step, so two requests arriving at the same instant can't both slip through. When someone runs out, turn them away with a clear 'too many requests' response instead of doing the work. Forget about users who go quiet for a while, so we aren't storing counts for people who have left. Put this check right at the entrance, before any real work begins, so a flood of traffic never reaches the rest of the system.",
      a2: "Wrap the counter in one small component with a single question: is this user allowed right now? Each time it's asked, it does the whole check in one step in Redis — see how many requests the user has left, refill a little based on how much time has passed, subtract one, and answer yes or no. Store each person's remaining count and the time it was last topped up under their own key. Clear those keys automatically after a couple of quiet minutes so old counts don't pile up. Plug this component in at the very front of the gateway, before requests reach their handlers: find out who is asking, ask whether they are allowed, and if not, reply 'too many requests' along with when to try again. Wire it in as early as possible so blocked traffic never uses up real resources.",
      a3: "The architect and builder set up a per-user request allowance kept in Redis, refilled gradually, checked in a single step, and placed at the front of the gateway. First, the plan leans on the clock to decide when to refill, but clocks can drift or jump backward — if that happens, people could be handed more requests than they should get. Tie the refill to a clock that only ever moves forward. Second, forgetting a user after just two minutes is too quick: a normal person who pauses briefly comes back to a full allowance and can suddenly fire off a burst of requests. Hold onto their count for at least an hour instead. Third, when someone is turned away we should tell them exactly how long to wait before trying again, worked out from their own refill rather than a fixed guess. Approve with a few changes.",
    },
    'cache-invalidation': {
      label: 'Cache invalidation',
      user: 'Product catalog cache keeps serving stale prices after updates. Fix it.',
      a1: "Keep a fast copy of each price in Redis so pages load quickly, but treat the database as the real source of truth. The moment a price changes, immediately clear the fast copy for that product so the next shopper sees the new price, and also let each copy expire on its own after a few minutes as a safety net. Send out a short 'this price changed' notice whenever an update happens, and have the cache listen for those notices and drop the matching copy within a fraction of a second. Give each price a version tag so a browser can check whether the copy it holds is still current. Watch how often the cache is serving useful copies, and if that rate slips, start logging so we can see how far behind the updates are running.",
      a2: "Put one component in charge of prices. It keeps the quick-access copies in Redis for fast reads and falls back to the database for the true, versioned price whenever a copy is missing. Whenever a price is updated, a 'price changed' notice goes out, and a listener catches it and deletes the stale copy within a tenth of a second. Every response also carries a version tag so a browser can confirm its copy is still good. A small monitor tracks how often copies are being served successfully and writes detailed notes about how late updates are arriving whenever that success rate drops below a healthy level.",
      a3: "The architect and builder chose a fast Redis copy backed by the database, with change notices clearing stale prices the instant a price updates. First, letting a copy live for five minutes is risky on its own — if a change notice is delayed or the listener goes down, shoppers keep seeing an old price for minutes; add a shorter safety refresh and a fallback that reads straight from the database whenever clearing fails. Second, when a popular product's price is cleared, a flood of shoppers can all ask for the new price at once and overwhelm the database; let the first request fetch it and have the rest wait for that single answer. Third, don't wait for the overall success rate to sag before reacting; watch how late the updates are running and raise a flag as soon as they approach the limit. Approve with a few changes.",
    },
    'feature-flag': {
      label: 'Feature flags',
      user: 'Build a feature-flag service supporting % rollout, user targeting, and instant kill-switch.',
      a1: "Build a lightweight service that stores each on/off switch and its rules — what share of users should see it, which specific groups to target, and a master 'off' override. Keep the switches in fast memory so checks are near-instant, and keep a full history of changes in a database for auditing. To roll a feature out to a set percentage, run each user's ID through a fixed formula that always lands the same person in the same bucket, so their experience stays consistent. Push any 'turn it off now' change out to every app within half a second. Let each app keep its own copy of the rules, refreshing every few minutes or the moment a change is pushed, so day-to-day checks happen instantly on their side without calling home. Tag every change with a version so two edits at once can't clobber each other.",
      a2: "One service answers a single question: should this user see this feature? It first checks fast memory for the switch and its rules; if it isn't there, it reads from the database and keeps a copy for a minute. To decide who is included, it runs the user's ID through the same fixed formula every time to place them in a percentage bucket, then checks them against the targeting rules. A 'turn it off' change is broadcast to connected apps within half a second. Each app downloads the full rulebook every few minutes and also listens for instant change alerts, keeping a local copy so its own checks are immediate. Edits carry a version stamp, and before saving a change the service compares versions to make sure it isn't overwriting a newer one, then refreshes the stored copy.",
      a3: "The architect and builder designed a lightweight switch service with rules in fast memory, a change history in the database, consistent per-user bucketing, and instant off-switches. First, caching switch checks at the edge for a full minute is dangerous for the off-switch: even with a half-second broadcast, edge copies could keep a feature on for a whole minute during an incident — shorten that window sharply and clear the edge copies the instant the off-switch is flipped. Second, if a pushed change quietly fails to arrive, an app might not notice for several minutes; have apps retry with growing gaps and include the last version they saw, so the server can force a fresh download when they are out of date. Third, placing users into buckets by ID alone breaks if someone moves between target groups; fold the group and the feature into the formula and note that these shouldn't change once set. Approve with a few changes.",
    },
    'observability': {
      label: 'Tracing ingest',
      user: 'Ingest OpenTelemetry traces at 50k spans/sec, store 7-day window, query by trace_id.',
      a1: "Pick storage built for huge streams of small records that are written constantly but read only occasionally. Accept the incoming data in batches rather than one piece at a time, so the system can keep up with the very high volume. Group the stored data by day and automatically throw it away after a week, and organize it by its trace ID so looking up a single trace is almost instant. Only index the handful of fields people actually search on, since indexing everything would be wasteful. Expect a large amount of data at this rate, so compress it heavily once it is written. Finally, put a safety valve on the intake so that if data arrives faster than it can be stored, the backlog is capped instead of running the system out of memory.",
      a2: "Data comes in and is grouped into batches, then written in compressed daily blocks to cheap cloud storage that clears itself out after a week. A safety valve caps the backlog: if too much piles up, new data is briefly refused so memory stays safe. A lightweight lookup table remembers where each trace lives, updated on every write, so a search jumps straight to the right block instead of scanning everything, then unpacks just that block. At this volume, expect the compressed data to be a small fraction of its original raw size.",
      a3: "The prior two hops settled on day-by-day storage in cheap cloud buckets, batched intake, automatic week-long cleanup, and trace-ID lookups. First, keeping the full 'where does each trace live' table in memory won't scale — at this volume it would balloon far past what memory can hold; keep only the most recent entries close at hand and store the rest in a separate lookup service. Second, batching by a very small size can stall if a single record is larger than the batch; raise the batch size and add a short timer so nothing waits too long. Third, simply refusing extra data may not be enough to protect the systems feeding in; watch how far behind the intake is falling and add more capacity automatically when the backlog grows. Approve with a few changes.",
    },
  };

  const tokenize = text => text.trim().split(/\s+/).map(t => ({ t }));
  const countTokens = (text) => Math.ceil(text.length / 4);

  // This example uses Anthropic's documented cache economics: a cache read costs
  // 10% of fresh input and the first cache write costs 125%. A realistic shared
  // system/tools/project prefix keeps every hop above the provider's cache minimum.
  const SHARED_PREFIX_TOKENS = 1400;
  const CACHE_READ_RATE = 0.10;
  const CACHE_WRITE_RATE = 1.25;

  const TASKS = Object.entries(RAW).map(([id, r]) => {
    const a1 = tokenize(r.a1);
    const a2 = tokenize(r.a2);
    const a3 = tokenize(r.a3);
    const a1Tokens = countTokens(r.a1);
    const a2Tokens = countTokens(r.a2);
    const a3Tokens = countTokens(r.a3);
    const sharedTokens = SHARED_PREFIX_TOKENS + countTokens(r.user);

    // Every call still receives the complete context available at that hop.
    const baseline = {
      call1: sharedTokens,
      call2: sharedTokens + a1Tokens,
      call3: sharedTokens + a1Tokens + a2Tokens,
    };
    baseline.total = baseline.call1 + baseline.call2 + baseline.call3;

    // Cache-adjusted cost, expressed in fresh-input-token equivalents. The first
    // hop pays the cache-write premium; later hops read the stable prefix cheaply.
    const cachePlan = [
      {
        label: 'CACHE WARM-UP',
        detail: 'Full context is sent; the stable prefix is stored at the provider’s one-time write rate.',
        total: baseline.call1,
        cached: 0,
        fresh: baseline.call1,
        cost: Math.round(sharedTokens * CACHE_WRITE_RATE),
      },
      {
        label: 'CACHE READ',
        detail: 'Shared context is reused; only the architect’s new work is billed fresh.',
        total: baseline.call2,
        cached: sharedTokens,
        fresh: a1Tokens,
        cost: Math.round(sharedTokens * CACHE_READ_RATE + a1Tokens),
      },
      {
        label: 'CACHE READ',
        detail: 'The byte-stable history is reused; only the builder’s new work is fresh.',
        total: baseline.call3,
        cached: sharedTokens + a1Tokens,
        fresh: a2Tokens,
        cost: Math.round((sharedTokens + a1Tokens) * CACHE_READ_RATE + a2Tokens),
      },
    ];
    const cacheAdjustedTotal = cachePlan.reduce((sum, hop) => sum + hop.cost, 0);
    const pctSaved = Math.round((1 - cacheAdjustedTotal / baseline.total) * 100);

    return {
      id, label: r.label, user: r.user,
      a1, a2, a3,
      a1Raw: r.a1, a2Raw: r.a2, a3Raw: r.a3,
      a1Tokens, a2Tokens, a3Tokens,
      baseline, cachePlan, cacheAdjustedTotal, pctSaved,
    };
  });

  return { TASKS };
})();
