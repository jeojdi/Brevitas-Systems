"""How prompt-cache reuse degrades as retrieved content churns — head vs tail.

THE CLAIM THIS SUPPORTS, and the one it does not.

NOT: "$173.70 -> $20.33". That pair is the churn = 1.0 endpoint of a synthesised
arm and was repeatedly quoted as a measured workload. It is retracted (R6 in
splice/docs/RETRACTIONS.md).

NOT: "nobody has measured this". arXiv 2601.06007 ("Don't Break the Cache",
Lumer et al., Jan 2026) evaluated prompt caching across OpenAI, Anthropic and
Google over 500+ agent sessions and found end-of-prompt placement beats naive
full-context caching. Eight months earlier, at 25x the sample (R7).

WHAT SURVIVES is the ELASTICITY: head placement couples your bill to retrieval
stability, tail placement decouples it. That is a statement about the SHAPE of
the curve, and a shape is what a buyer can act on -- it says which knob to turn
and how much it is worth.

TWO CORRECTIONS TO THE EARLIER SWEEP, both of which made head placement look
better than it is:

 1. SCORING BY LENGTH, NOT CONTENT. The previous head arm computed a block as an
    integer token count -- `H + sum(F[i] + 2 for i in sel)` -- and tested
    `blk == prev_blk`. Two different retrieval selections with the same total
    token length scored as a cache HIT. Collisions were rare (its churn-1.0 head
    figure, 9.61%, sat 0.05pp from a content-exact 9.56%) and the bias favoured
    head placement, so it UNDERSTATED the penalty. Here a block is hashed by its
    CONTENT.
 2. THE WRITE RATE WAS THE CHEAP TIER. The earlier billing model priced cache
    writes at 1.25x base. The field data for this workload class says 88.8% of
    its writes buy the 1-HOUR tier, priced at 2x. Head placement pays that write
    premium on every turn it invalidates, so the cheap rate understated its cost
    too. Both rates are reported.

MODEL. A prompt is [ header | retrieved facts | conversation ] for HEAD
placement and [ header | conversation | retrieved facts ] for TAIL. Each turn,
a `churn` fraction of the retrieved set is resampled. Reuse is the share of
prompt tokens that fall inside the longest common CONTENT prefix with the
previous turn, rounded down to the provider's cache block granularity.
"""
from __future__ import annotations
import argparse, hashlib, json, random

# provider-ish defaults; see prefixguard.PROVIDERS for the sourced table
BLOCK = 1024          # cache granularity, tokens
FLOOR = 1024          # minimum cacheable prefix, tokens


def _h(x: str) -> str:
    return hashlib.blake2b(x.encode(), digest_size=8).hexdigest()


def build(header_tok, facts, conv_tok, placement):
    """Return a list of (content_hash, n_tokens) blocks in prompt order.

    CONTENT-HASHED, not length-summed. That is correction 1.
    """
    parts = [(_h("HEADER"), header_tok)]
    fact_parts = [(_h(f"F{fid}"), ftok) for fid, ftok in facts]
    conv_parts = [(_h("CONV"), conv_tok)]
    if placement == "head":
        return parts + fact_parts + conv_parts
    return parts + conv_parts + fact_parts


def reuse_fraction(prev, cur):
    """Share of `cur`'s tokens inside the longest common content prefix,
    truncated to block granularity and to the minimum cacheable length."""
    total = sum(t for _, t in cur)
    common = 0
    for (ph, pt), (ch, ct) in zip(prev, cur):
        if ph != ch or pt != ct:
            break
        common += ct
    cacheable = (common // BLOCK) * BLOCK
    if cacheable < FLOOR:
        cacheable = 0
    return (cacheable / total) if total else 0.0, cacheable, total


def sweep(churns, k, turns, header_tok, fact_tok, conv_tok, pool, seed,
          order="stable"):
    """`order` is the mechanism the first version of this script hid.

    A retriever returning the SAME facts in a DIFFERENT order destroys the cached
    prefix exactly as thoroughly as replacing them, because a prefix is a
    sequence, not a set. The first version resampled survivors with
    `rng.sample`, which reshuffles, so every churn > 0 collapsed to the header
    and the curve went flat -- and the flatness was the reshuffle, not the churn.
      order="stable"   survivors keep their relative order, new facts append
      order="reranked" the whole selection is re-ordered every turn
    Both are real retriever behaviours, and the gap between them is measurable.

    NOT OURS, AND THE CITATION WAS ALREADY IN OUR OWN NOTES.
    CacheWeaver (arXiv 2606.19667, 2026-06-18), "Cache-Aware Evidence Ordering
    for Efficient Grounded RAG Inference", says it more precisely than we did:
    "adjacent queries may retrieve overlapping evidence in different orders, so
    set overlap does not become reusable prefix overlap." It also SOLVES it --
    cache-aware reordering, 20-33% relative latency improvement -- two months
    before we measured it. Worse, CacheWeaver was already listed as prior art in
    splice/docs/build/HEADTAIL.md; the citation was in hand and the claim was
    still produced as if new. Same failure as citing a doc instead of the test.
    This arm therefore REPRODUCES a published result. That is still worth having
    -- it is the mechanism the CI check has to model -- but it is not a finding.
    """
    rows = []
    for placement in ("head", "tail"):
        for churn in churns:
            rng = random.Random(seed)
            sel = rng.sample(range(pool), k)
            prev = None
            reused = kept = tot = 0
            for _ in range(turns):
                cur = build(header_tok, [(i, fact_tok) for i in sel], conv_tok,
                            placement)
                if prev is not None:
                    _f, cacheable, total = reuse_fraction(prev, cur)
                    kept += cacheable
                    tot += total
                    reused += 1
                prev = cur
                n_change = int(round(churn * k))
                if n_change:
                    # drop a random subset but PRESERVE the order of survivors
                    drop = set(rng.sample(sel, n_change))
                    keep = [x for x in sel if x not in drop]
                    fresh = [x for x in rng.sample(range(pool), k + n_change)
                             if x not in keep][:n_change]
                    sel = keep + fresh
                    if order == "reranked":
                        rng.shuffle(sel)
            rows.append({"placement": placement, "churn": churn,
                         "order": order,
                         "reuse_pct": 100.0 * kept / tot if tot else 0.0,
                         "turns_scored": reused})
    return rows


def bill(reuse_pct, total_tok, turns, write_rate, read_rate=0.10, base=1.0):
    """Cost per session in base-input-token units. `write_rate` is the tier
    multiplier: 1.25x is the cheap tier, 2.0x is the 1-hour tier that the field
    data says this workload class actually buys 88.8% of the time."""
    r = reuse_pct / 100.0
    per_turn = total_tok * (r * read_rate + (1 - r) * write_rate * base)
    return per_turn * turns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5, help="retrieved facts per turn")
    ap.add_argument("--turns", type=int, default=12)
    ap.add_argument("--header-tokens", type=int, default=9000)
    ap.add_argument("--fact-tokens", type=int, default=420)
    ap.add_argument("--conv-tokens", type=int, default=2600)
    ap.add_argument("--pool", type=int, default=400)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    churns = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    rows = []
    for order in ("stable", "reranked"):
        rows += sweep(churns, a.k, a.turns, a.header_tokens, a.fact_tokens,
                      a.conv_tokens, a.pool, a.seed, order)
    total_tok = a.header_tokens + a.k * a.fact_tokens + a.conv_tokens
    for r in rows:
        r["cost_cheap_write_1.25x"] = bill(r["reuse_pct"], total_tok, a.turns, 1.25)
        r["cost_1h_tier_2.0x"] = bill(r["reuse_pct"], total_tok, a.turns, 2.0)

    if a.json:
        print(json.dumps({"rows": rows, "block": BLOCK, "floor": FLOOR,
                          "total_tokens_per_turn": total_tok,
                          "note": "content-hashed blocks; both write tiers"},
                         indent=1))
        return 0

    print(f"  k={a.k} facts/turn, {a.turns} turns, {total_tok} tokens/turn, "
          f"block={BLOCK}, floor={FLOOR}")
    print(f"\n  {'churn':>6} {'head reuse':>11} {'tail reuse':>11} "
          f"{'head cost @2x':>14} {'tail cost @2x':>14} {'head/tail':>10}")
    by = {(r["placement"], r["churn"], r["order"]): r for r in rows}
    for c in churns:
        h, t = by[("head", c, "stable")], by[("tail", c, "stable")]
        ratio = h["cost_1h_tier_2.0x"] / t["cost_1h_tier_2.0x"]
        print(f"  {c:>6.1f} {h['reuse_pct']:>10.2f}% {t['reuse_pct']:>10.2f}% "
              f"{h['cost_1h_tier_2.0x']:>14,.0f} {t['cost_1h_tier_2.0x']:>14,.0f} "
              f"{ratio:>9.2f}x")
    h0, h1 = by[("head", 0.0, "stable")], by[("head", 1.0, "stable")]
    t0, t1 = by[("tail", 0.0, "stable")], by[("tail", 1.0, "stable")]
    print(f"\n  ELASTICITY — the finding:")
    print(f"    head placement: {h0['reuse_pct']:.2f}% -> {h1['reuse_pct']:.2f}% "
          f"reuse as churn goes 0 -> 1  (swing {h0['reuse_pct']-h1['reuse_pct']:.1f}pp)")
    print(f"    tail placement: {t0['reuse_pct']:.2f}% -> {t1['reuse_pct']:.2f}% "
          f"                        (swing {t0['reuse_pct']-t1['reuse_pct']:.1f}pp)")
    print(f"    Head couples the bill to retrieval stability; tail decouples it.")
    print(f"    That shape, not any dollar pair, is what a buyer can act on.")
    print(f"\n  ORDER STABILITY — a second, separable mechanism:")
    print(f"    {'churn':>6} {'head stable':>12} {'head reranked':>14} {'penalty':>9}")
    for c in churns:
        hs = by[("head", c, "stable")]["reuse_pct"]
        hr = by[("head", c, "reranked")]["reuse_pct"]
        print(f"    {c:>6.1f} {hs:>11.2f}% {hr:>13.2f}% {hs-hr:>8.1f}pp")
    print(f"    A retriever returning the SAME facts in a DIFFERENT order destroys")
    print(f"    the prefix as thoroughly as replacing them: a prefix is a sequence,")
    print(f"    not a set. This REPRODUCES CacheWeaver (arXiv 2606.19667, Jun 2026),")
    print(f"    which states it as 'set overlap does not become reusable prefix")
    print(f"    overlap' and also solves it. Not a finding -- a mechanism to model.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
