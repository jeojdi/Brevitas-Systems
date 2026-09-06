# Provider constants — the source of record

`prefixguard.py`'s `PROVIDERS` table is **checked against this file** by
`test_prefixguard.py`. Editing a constant in the code without editing it here
fails the suite. That is deliberate: a wrong floor makes the gate exit 0 on a
change that destroys the entire cache, and the vendors themselves document that
this failure is silent at runtime ("no error is returned").

**Last verified: 2026-08-26.** These change. The tool warns after 90 days.

Format below is fixed — `| model-key | floor |` — because the test parses it.

## anthropic

Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching

> "Shorter prompts cannot be cached, even if marked with `cache_control`. Any
> requests to cache fewer than this number of tokens will be processed without
> caching, **and no error is returned**."

Up to 4 explicit cache breakpoints. Breakpoint-granular (no block rounding).
The system checks at most 20 positions per breakpoint.

| model-key | floor |
|---|---|
| opus-5 | 512 |
| fable-5 | 512 |
| mythos-5 | 512 |
| opus-4.8 | 1024 |
| sonnet-5 | 1024 |
| sonnet-4.6 | 1024 |
| sonnet-4.5 | 1024 |
| opus-4.1 | 1024 |
| opus-4 | 1024 |
| sonnet-4 | 1024 |
| mythos-preview | 2048 |
| opus-4.7 | 2048 |
| haiku-3.5 | 2048 |
| opus-4.6 | 4096 |
| opus-4.5 | 4096 |
| haiku-4.5 | 4096 |

## openai

Source: https://developers.openai.com/api/docs/guides/prompt-caching

1,024 tokens for GPT-5.6 and later; 2,048 visible input tokens for earlier
models. Pre-5.6 rounds reported `cached_tokens` down to a multiple of 128;
GPT-5.6+ reports the exact eligible boundary. `prompt_cache_key` influences
routing only, so the hit is stochastic.

| model-key | floor |
|---|---|
| gpt-5.6+ | 1024 |
| pre-5.6 | 2048 |

## gemini

Source: https://ai.google.dev/gemini-api/docs/caching

Implicit caching is enabled by default for 2.5 and newer. No prefix-matching
mechanism is documented, so segment arithmetic is weaker here than for
Anthropic.

| model-key | floor |
|---|---|
| 2.5-flash | 2048 |
| 2.5-pro | 2048 |
| 3.1-pro-preview | 4096 |
| 3.5-flash | 4096 |
| 3.6-flash | 4096 |
| 3.7-flash | 4096 |
