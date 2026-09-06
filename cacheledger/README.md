# cacheledger

**What is prompt caching actually doing to your bill?**

Every provider bills cache reads, cache writes and fresh input at different rates
and hands you the counts in every single response. Almost nobody adds them up.
Point this at your own logs:

```
$ cacheledger receipts.jsonl --from anthropic --rates rates.json

  receipts           13,278 in 1,795 session(s)
  prompt tokens      3,597,406,846
    cache read       3,508,730,497  (97.53% realized hit rate, token-weighted)
    cache write      88,464,334
    fresh input      212,015
  1h tier share      92.6% of writes
  billed             $1,569.28
  without caching    $10,792.22   -> saved 85.5% ($9,222.94)
  WASTED WRITE       $0.12 premium on 153,554 tokens never read back, in 15 session(s)
  TTL cliff          gap(s)          pairs   lost   loss rate
                           0-300       10711     18      0.2%
                         300-1800        552     21      3.8%
                        1800-3600         84      4      4.8%
                        3600-7200         38     20     52.6%
                        7200-inf          98     49     50.0%
```

That is real output over 3.6 billion prompt tokens of production coding-agent
traffic. Two things in it are not on any dashboard.

**The wasted write premium.** A cache write costs 1.25× or 2× the input rate. If
nothing reads it back, you paid a premium for nothing — marking a prefix that is
never reused is *strictly worse* than not caching it. No vendor reports this, and
it is the one number that tells you to stop doing something.

**The TTL cliff, for your arrival pattern.** Loss rate runs ~0–5% across every
gap under an hour and then jumps to 52.6% past it. That is what decides whether the
longer TTL tier earns its higher write multiplier — and it is a property of your
traffic, not of the provider.

## Install

Python 3.10+. **No dependencies.** Copy the file.

## Input

`--schema` prints the contract. It is Anthropic's own `usage` shape, so raw logs
pipe straight in. Adapters: `--from anthropic | openai | claude-code`.

The OpenAI adapter exists because of one trap: OpenAI reports `cached_tokens` as
a **subset** of `prompt_tokens`, while Anthropic reports reads **alongside**
input. Adding them the same way inflates the hit rate — in the suite's fixture,
from 80% to 44% prompt-inflated. Feeding Anthropic-shaped data through
`--from openai` is detected and refused rather than silently mis-summed.

## Four things it refuses to do

**It will not guess prices.** `--rates` is required. Rates differ per model, per
tier and per provider, they change, and a wrong rate card turns every dollar
figure into fiction while looking exactly as authoritative. Token counts are the
evidence; dollars are derived under a rate card you supplied, which is echoed
into the JSON output.

**It will not infer a session boundary.** Cache locality is a property of who
shares a prefix. If your receipts carry no session id, say so with
`--single-session`.

**It will not report a hit rate over too few tokens.** Below `--min-tokens` the
ratio is noise, and it refuses rather than printing a number with a caveat nobody
reads.

**It will not count a replayed turn twice.** Agent transcripts write the same
assistant turn more than once — streaming, resume, replay — and every copy
carries the *same* provider receipt. On the corpus above, **55.1% of raw lines
were duplicates**. Anyone computing cache economics from agent logs without
deduping on request id over-counts their bill by roughly 2.2×. It dedupes and
tells you how many it dropped.

That last one was found by cross-checking against an independent implementation
on the same corpus: the two agreed to **0.01pp on the hit rate** and disagreed by
**2.1× on absolute tokens**. Ratios survive duplication. Dollars do not, and
dollars are the output.

## Use it as a CI gate

```
cacheledger receipts.jsonl --from anthropic --rates rates.json \
    --min-hit-rate 0.90 --max-wasted-usd 25
```

Exit **0** healthy · **1** a threshold you set was breached · **3** refused to
measure. Exit 3 is never exit 1: "I could not measure" and "I measured, it is
bad" are different facts, and conflating them is how a gate gets switched off.

## Tests

`python3 test_cacheledger.py` — 16 checks. Arithmetic is verified against
**hand-computed** values rather than against the tool's own output; every refusal
asserts; all three exit codes are exercised through the CLI.

Mutation-tested — each of these turns the suite red:

| mutation | caught by |
|---|---|
| OpenAI adapter double-counts cached tokens | A3 |
| hit rate computed per-request instead of per-token | A3 |
| wasted premium charges the full write, not the premium | B |
| counterfactual priced at the read rate | A2 |
| missing rate-card entry silently becomes $0 | R1 |

A check that has never been observed to fail is not evidence.

## What it does not do

It does not predict your hit rate, and for OpenAI it cannot: `prompt_cache_key`
influences routing but does not pin a request to a machine, so the hit is
stochastic. This is a ledger of what already happened, not a forecast.
