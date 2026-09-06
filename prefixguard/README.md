# prefixguard

Fails your build when a prompt change breaks provider prefix caching — and, just as importantly,
**does not** fail it when the change costs nothing.

```
$ prefixguard --before old_prompt.txt --after new_prompt.txt \
      --provider anthropic --tokenizer hf:Qwen/Qwen2.5-0.5B-Instruct \
      --breakpoint-tokens 5000 --model haiku-4.5

  tokens             5710 -> 5724, common prefix 0
  cached tokens      5000 -> 0   (lost 5000)
  RECOVERY           0.0%
  verdict            TOTAL LOSS — the change lands in the first cached segment
  a byte diff        would report 'stable for 0 bytes', which says nothing about
                     the actual loss of 5000 cached tokens
$ echo $?
1
```

Exit codes: **0** within tolerance · **1** recovery below `--min-recovery` · **3** refused (see below).

## Why not just diff the prompts?

Because a byte diff is wrong in both directions, and we measured both.

**Not sufficient.** With a single breakpoint, ten different perturbations — leading space, trailing
space, CRLF, NFC/NFD, case flip, single-token insertion at head, middle *and* tail — every one gave
**0% recovery**. Inside one cached block, position is irrelevant: any byte kills all of it. So
"stable for 4,182 bytes" tells you nothing, because a difference at byte 4,183 destroys all 4,182.

**Not necessary.** A change *past* the last breakpoint costs exactly zero, and a byte diff flags it
anyway. That is a false-positive generator, and false positives are how a CI gate gets switched off.

And the part naive arithmetic misses — the **floor**. With four breakpoints, invalidation is
breakpoint-granular but truncated from below by the model's minimum cacheable length:

| perturbed segment | 1 | 2 | 3 | 4 | past last breakpoint |
|---|---|---|---|---|---|
| naive `(j−1)/n` predicts | 0% | 25% | 50% | 75% | 100% |
| **measured** | 0% | **0%** | 50.4% | 74.9% | 100% |

Segment 2 predicts 25% and delivers **0%**, because its ~2,194 surviving tokens sit below the
model's ~4,096-token floor. Segment 3's ~4,388 survivors clear it by 292.

```
recovery = (j−1)/n   if   (j−1)/n · L ≥ floor(model)
         = 0         otherwise
```

## Install

```
pip install -r requirements.txt
```

`transformers` is the only hard requirement (it pulls a tensor backend, and the first run fetches
the tokenizer from huggingface.co). `tiktoken` is needed only for `--tokenizer tiktoken:<encoding>`.
On an air-gapped machine, pre-warm the HF cache or pass a local path — the tool exits **3** rather
than measuring with the wrong instrument.

## Three things it refuses to do

**It will not fall back to a byte diff.** If a real tokenizer for the target model is unavailable it
exits **3**. Falling back would silently turn it into the instrument it exists to replace.

**It will not guess your model's minimum-cacheable floor.** `--model` is required and exits **3**
on an unknown value; `--list-models` prints the table. Anthropic's floors span 512 to 4,096 across
the current line-up, and an earlier version of this tool collapsed four documented tiers to two,
which set 2,048 for `claude-haiku-4-5` whose real floor is 4,096. That error direction produces a
**silent false negative** — the gate exits 0 while the whole cached prefix is in fact destroyed —
and the vendor documents that the runtime failure is silent too ("no error is returned"). A gate
that says "fine" when the cache is gone is worse than no gate.

**It will not guess where your breakpoints are.** `--breakpoint-tokens` is required. The cost of a
change depends entirely on where the cached region *ends*, and an earlier version of this tool
spread breakpoints evenly across the whole prompt — which implicitly puts the last one at the end,
so *any* change anywhere reported total loss. That is precisely the false positive above, and the
test caught it before it shipped.

## Prior art, stated plainly

The **finding** is not ours. *"Don't Break the Cache"* (arXiv 2601.06007, Jan 2026) evaluated prompt
caching across OpenAI, Anthropic and Google over 500+ agent sessions and found that end-of-prompt
placement beats naive full-context caching.

The **tool** appears not to exist. No observability vendor surveyed alerts on cache-hit-rate
regression: LangSmith has no cached-token metric at all; Langfuse ingests the tokens but ships only
generic aggregation alerts; Helicone caches *responses* at its own edge, which its docs distinguish
from provider prefix caching; Grafana's Anthropic integration ships three alert rules, all
cost/volume. The closest thing is `sernote/audit-prompt-caching` (MIT), whose
`prefix_stability_check.py` is a **byte diff** — `tiktoken|count_tokens|min_cacheable` appear nowhere
in its executable code, and its own reference notes decline to assert token minimums.

So the differentiator is narrow and specific: **tokenizer- and block-boundary-aware**, rather than
"a prefix check".

## Tests

`python3 test_prefixguard.py` — 20 checks: the cases a byte diff gets wrong, all five refusal
paths, all three documented exit codes exercised through the CLI, and a vacuity guard that disables
the floor rule and requires the answer to change.

Expectations are derived from actual token counts rather than hardcoded, because three earlier
fixture failures were all arithmetic slips in the test. The provider constants are **parsed from
`PROVIDER_SOURCE.md`** and diffed against the code, so changing a floor without changing the source
of record fails the suite — a mutation test found that reverting `haiku-4.5` to 2,048 was invisible
to every behavioural check.

Mutation-tested: deleting the floor rule, reverting the haiku floor, and switching the byte diff
back to characters each turn the suite red. A check that has never been observed to fail is not
evidence.
