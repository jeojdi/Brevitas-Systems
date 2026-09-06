"""prefixguard — does this prompt change break provider prefix caching, and what does it cost?

WHY THIS IS NOT A BYTE DIFF, WHICH IS THE ONLY REASON IT IS WORTH WRITING.
The public tooling (sernote/audit-prompt-caching's `prefix_stability_check.py`) reports the BYTE
OFFSET of the first difference. That is wrong in both directions, and we measured both:

  NOT SUFFICIENT.  With a single breakpoint, ten different perturbations -- leading space, trailing
  space, CRLF, NFC/NFD, case flip, single-token insertion at head, middle AND tail -- every one gave
  0% recovery. Inside one block, position is irrelevant: any byte kills all of it. So "stable for
  4,182 bytes" tells you nothing, because a difference at byte 4,183 destroys all 4,182.

  NOT NECESSARY.  A difference PAST the last breakpoint costs exactly zero, and a byte diff flags it
  anyway. That is a false-positive generator, and false positives are how a CI gate gets turned off.

THE RULE, measured rather than assumed (Brevitas-Systems/docs/FIELD_CACHE_MEASUREMENT.md:148-186):

    recovery = (j-1)/n   if   (j-1)/n * L >= floor(model)
             = 0         otherwise

where the first difference lands in breakpoint segment j of n, and L is the prefix token count.
The floor truncation is the part naive arithmetic misses. Our four-breakpoint measurement:

    segment              1        2        3        4     past last
    naive (j-1)/n       0%      25%      50%      75%        100%
    MEASURED            0%    ** 0% **  50.4%    74.9%       100%

Segment 2 predicts 25% and measures 0% because its ~2,194 surviving tokens sit below the model's
~4,096-token floor. Segment 3's ~4,388 survivors clear it by 292.

NO BYTE FALLBACK, EVER. If a real tokenizer for the target model is unavailable this tool REFUSES
and exits 3. Falling back to bytes would silently turn it into the instrument it exists to replace.
"""
from __future__ import annotations
import argparse, datetime as _dt, json, sys, unicodedata
from dataclasses import dataclass, field, asdict

# Provider rules. Every number here is quoted from vendor documentation in
# splice/docs/build/PREFIX_PRODUCT.md §3, which cites the source page for each.
# A wrong constant here makes the tool worse than nothing, so they are named,
# separated, and none of them are guessed.
#
# THERE IS NO DEFAULT FLOOR, AND THAT IS THE THIRD DELIBERATE REFUSAL.
# The previous table collapsed Anthropic's four documented tiers to two
# ({"default": 1024, "haiku": 2048}), which made 512 and 4,096 UNREACHABLE.
# `--model-hint haiku` therefore set 2,048 for claude-haiku-4-5, whose real
# floor is 4,096 -- and the error direction produces a SILENT FALSE NEGATIVE:
# the gate exits 0 while the real cost is the entire cached prefix. A gate that
# says "fine" when the cache is destroyed is worse than no gate, because it
# launders the failure. Vendor docs say the same thing about the runtime
# behaviour ("no error is returned"), which is the whole reason this tool exists.
# So the model is REQUIRED and unknown models are refused, exactly as
# breakpoints are refused rather than guessed.
LAST_VERIFIED = "2026-08-26"
STALE_AFTER_DAYS = 90

PROVIDERS = {
    "anthropic": {
        "max_breakpoints": 4,
        "granularity_tokens": 1,       # breakpoint-granular, not block-rounded
        "models": {
            # PREFIX_PRODUCT.md §3.1, from platform.claude.com prompt-caching
            "opus-5": 512, "fable-5": 512, "mythos-5": 512,
            "opus-4.8": 1024, "sonnet-5": 1024, "sonnet-4.6": 1024,
            "sonnet-4.5": 1024, "opus-4.1": 1024, "opus-4": 1024, "sonnet-4": 1024,
            "mythos-preview": 2048, "opus-4.7": 2048, "haiku-3.5": 2048,
            "opus-4.6": 4096, "opus-4.5": 4096, "haiku-4.5": 4096,
        },
        "note": "up to 4 explicit breakpoints; automatic caching moves the last "
                "breakpoint forward as the conversation grows; the system checks "
                "at most 20 positions per breakpoint, and a miss one position "
                "outside that window stops the search",
    },
    "openai": {
        "max_breakpoints": 1,          # automatic, no user-placed breakpoints
        "granularity_tokens": 128,     # pre-5.6 rounds down to a multiple of 128
        "models": {
            # §3.2: 1,024 for GPT-5.6 and later; 2,048 for earlier models.
            "gpt-5.6+": 1024,
            "pre-5.6": 2048,
        },
        "granularity_by_model": {"gpt-5.6+": 1, "pre-5.6": 128},
        "note": "automatic prefix caching; pre-5.6 reports cached_tokens rounded "
                "down to a multiple of 128, GPT-5.6+ reports the exact eligible "
                "boundary. prompt_cache_key only influences routing, so the hit "
                "is stochastic: this tool bounds the eligible prefix, it does "
                "NOT predict the hit",
    },
    "gemini": {
        "max_breakpoints": 1,
        "granularity_tokens": 1,
        "models": {
            # §3.3: 4,096 for 3.7/3.6/3.5 Flash and 3.1 Pro Preview; 2,048 for 2.5.
            "3.7-flash": 4096, "3.6-flash": 4096, "3.5-flash": 4096,
            "3.1-pro-preview": 4096,
            "2.5-flash": 2048, "2.5-pro": 2048,
        },
        "note": "implicit caching is on by default for 2.5+; no prefix-matching "
                "mechanism is documented, so segment arithmetic is weaker here "
                "than for Anthropic. Explicit caching bills storage per unit "
                "time, which this tool does NOT model",
    },
}


class ModelUnknown(RuntimeError):
    """Raised rather than assuming a minimum-cacheable floor."""
    pass


def resolve_model(provider: str, model: str | None) -> tuple[int, int]:
    """(floor_tokens, granularity_tokens) for an EXPLICITLY named model."""
    rules = PROVIDERS[provider]
    known = rules["models"]
    if model not in known:
        raise ModelUnknown(
            f"unknown {provider} model {model!r}. The minimum-cacheable floor "
            f"differs by {len(set(known.values()))}x across this provider's "
            f"line-up and a wrong floor silently passes a change that destroys "
            f"the whole cache, so it is not defaulted. Known: "
            + ", ".join(f"{k}={v}" for k, v in sorted(known.items(),
                                                      key=lambda kv: (kv[1], kv[0]))))
    g = rules.get("granularity_by_model", {}).get(model, rules["granularity_tokens"])
    return known[model], g


class TokenizerUnavailable(RuntimeError):
    pass


class BreakpointsUnknown(RuntimeError):
    """Raised rather than assuming where the cached region ends."""
    pass


def load_tokenizer(spec: str):
    """Return (encode_fn, name). Refuses rather than degrading to bytes.

    `spec` is either 'hf:<model-id>' or 'tiktoken:<encoding>'. There is
    deliberately no default and no fallback: which tokenizer you use changes
    every number this tool prints, so it must be stated.
    """
    if spec.startswith("hf:"):
        try:
            from transformers import AutoTokenizer
        except Exception as e:                                    # noqa: BLE001
            raise TokenizerUnavailable(f"transformers not importable: {e}") from e
        try:
            tok = AutoTokenizer.from_pretrained(spec[3:])
        except Exception as e:                                    # noqa: BLE001
            # This sat OUTSIDE the try, so the single most likely stranger
            # failure -- no network, uncached model, typo'd id -- raised OSError,
            # escaped, and exited 1. CI reads 1 as "your change broke the cache".
            # Conflating "I could not measure" with "I measured, it is bad" is
            # the exact error the retractions ledger is about.
            raise TokenizerUnavailable(
                f"could not load tokenizer {spec[3:]!r} ({type(e).__name__}: {e}). "
                f"This usually means no network, no HF cache, or a wrong model id."
            ) from e
        return (lambda s: tok(s, add_special_tokens=False).input_ids), spec
    if spec.startswith("tiktoken:"):
        try:
            import tiktoken
        except Exception as e:                                    # noqa: BLE001
            raise TokenizerUnavailable(
                f"tiktoken not installed, so token counts for an OpenAI model "
                f"cannot be computed: {e}") from e
        enc = tiktoken.get_encoding(spec[9:])
        return enc.encode, spec
    raise TokenizerUnavailable(
        f"unrecognised tokenizer spec {spec!r}; use 'hf:<model-id>' or "
        f"'tiktoken:<encoding>'")


def common_prefix_len(a: list[int], b: list[int]) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


@dataclass
class Result:
    provider: str
    tokenizer: str
    tokens_before: int
    tokens_after: int
    common_prefix_tokens: int
    first_diff_token: int | None
    n_breakpoints: int
    segment_of_first_diff: int | None
    surviving_fraction_naive: float
    surviving_tokens_naive: int
    model: str
    min_cacheable_tokens: int
    floor_truncated: bool
    cached_tokens_before: int
    cached_tokens_after: int
    cached_tokens_lost: int
    recovery: float
    verdict: str
    byte_diff_would_say: str
    notes: list[str] = field(default_factory=list)


def analyse(before: str, after: str, provider: str, tokenizer_spec: str,
            bp_tokens, model: str | None = None,
            normalize: bool = False) -> Result:
    rules = PROVIDERS[provider]
    floor, granularity = resolve_model(provider, model)
    enc, tokname = load_tokenizer(tokenizer_spec)
    if normalize:
        before, after = (unicodedata.normalize("NFC", x) for x in (before, after))
    tb, ta = enc(before), enc(after)
    cp = common_prefix_len(tb, ta)
    identical = (tb == ta)
    notes = [rules["note"]]
    if provider == "anthropic" and not tokenizer_spec.startswith("anthropic:"):
        notes.append(
            "TOKENIZER IS A PROXY: Anthropic does not publish a local tokenizer, "
            "so every token count here comes from a different vocabulary than "
            "the one that will bill you. Treat the counts as approximate and the "
            "floor comparison as the load-bearing part.")

    # WHERE THE BREAKPOINTS ARE IS NOT GUESSABLE, AND GUESSING IT PRODUCES
    # EXACTLY THE FALSE POSITIVE THIS TOOL EXISTS TO KILL.
    # The first version of this function spread n breakpoints evenly over the
    # whole prompt, which implicitly puts the LAST breakpoint at the end of the
    # prompt. Under that model any difference anywhere reported total loss --
    # including a change AFTER the cached region, which costs nothing. The test
    # caught it on case A. So positions are required, not inferred:
    #   bp_tokens = explicit token offsets of the breakpoints, ascending.
    # The cached region is [0, bp_tokens[-1]). Content after it is not cached and
    # is free to change.
    L = len(tb)
    given = sorted(set(int(x) for x in bp_tokens))
    bps = [b for b in given if 0 < b <= L]
    dropped = [b for b in given if b not in bps]
    if dropped:
        # Silently discarding an out-of-range breakpoint MOVES the end of the
        # cached region and therefore every number below it -- which is a guess,
        # made without saying so, in a tool whose second stated refusal is that
        # it will not guess where the breakpoints are. It also fires on the case
        # most likely to break caching: a prompt that SHRANK below a configured
        # breakpoint.
        if bps and max(given) > L:
            raise BreakpointsUnknown(
                f"breakpoint(s) {dropped} lie past the end of a {L}-token "
                f"prompt. Either the prompt shrank below a configured "
                f"breakpoint -- which is itself a cache-breaking change -- or "
                f"the offsets are stale. Refusing to silently relocate the end "
                f"of the cached region from {max(given)} to {max(bps)}.")
        notes.append(f"ignored non-positive breakpoint offsets {dropped}")
    if not bps:
        raise BreakpointsUnknown(
            f"no usable breakpoint positions for a {L}-token prompt (got "
            f"{list(bp_tokens)!r}). The cost of a change depends entirely on "
            f"where the cached region ENDS, so this tool will not assume it.")
    cached_end = bps[-1]
    if identical:
        seg = None
        surv_tokens = cached_end
    elif cp >= cached_end:
        # the change is past the last breakpoint: the whole cached region survives
        seg = len(bps) + 1
        surv_tokens = cached_end
    else:
        # survives up to the last breakpoint at or before the first difference
        below = [b for b in bps if b <= cp]
        seg = len(below) + 1
        surv_tokens = below[-1] if below else 0
    surv_frac = surv_tokens / max(cached_end, 1)
    # THE FLOOR RULE. This is the whole point of the tool.
    floor_truncated = (not identical) and 0 < surv_tokens < floor
    recovery = 0.0 if floor_truncated else surv_frac
    if identical:
        recovery = 1.0
    n = len(bps)
    if n > rules["max_breakpoints"]:
        notes.append(f"{n} breakpoints given but {provider} allows at most "
                     f"{rules['max_breakpoints']}; the extra ones cannot exist")

    g = granularity
    cached_before = (cached_end // g) * g if cached_end >= floor else 0
    cached_after_raw = surv_tokens
    cached_after = (cached_after_raw // g) * g if cached_after_raw >= floor else 0

    # NOT CACHEABLE IN THE FIRST PLACE IS NOT A REGRESSION.
    # The prompt's whole cached region sits below the floor, so nothing was ever
    # cached and the change costs exactly zero -- yet this reported "TOTAL LOSS"
    # while simultaneously printing cached_tokens_lost = 0. That is a false
    # positive on a change the tool itself computes as free, and false positives
    # are how a CI gate gets switched off (README §2). It was also the exact
    # configuration of the suite's own case B, which is why the floor rule went
    # untested: case B never reached the floor branch at all.
    if cached_before == 0 and not identical:
        floor_truncated = False
        recovery = 1.0
        notes.append(
            f"the cached region is {cached_end} tokens, below this model's "
            f"{floor}-token floor, so NOTHING was cacheable before the change "
            f"either. This is not a regression; it is a prompt that never "
            f"benefited from caching.")

    if identical:
        verdict = "IDENTICAL — full cache reuse"
    elif cached_before == 0:
        verdict = (f"NO COST — the prefix was never cacheable ({cached_end} "
                   f"tokens < {floor}-token floor)")
    elif recovery >= 0.999:
        verdict = "NO COST — the change falls past the last breakpoint"
    elif recovery == 0.0 and floor_truncated:
        verdict = (f"TOTAL LOSS — {surv_frac:.0%} of the prefix survives the "
                   f"breakpoint but {surv_tokens} tokens is below the "
                   f"{floor}-token floor, so nothing is cacheable")
    elif recovery == 0.0:
        verdict = "TOTAL LOSS — the change lands in the first cached segment"
    else:
        verdict = f"PARTIAL — {recovery:.1%} of the cached prefix survives"

    if identical:
        bd = "would report identical (agrees)"
    else:
        # REAL bytes. This counted CHARACTERS, so the straw man it quotes was
        # misstated by up to 3x on any non-ASCII prompt -- and the named prior
        # art reports actual bytes. Getting the comparison wrong in our own
        # favour is not a rhetorical flourish, it is the house error.
        bb, ab = before.encode("utf-8"), after.encode("utf-8")
        byte_cp = 0
        for x, y in zip(bb, ab):
            if x != y:
                break
            byte_cp += 1
        bd = (f"would report 'stable for {byte_cp} bytes', which "
              + ("OVERSTATES the loss — this change is free"
                 if recovery >= 0.999 else
                 "says nothing about the actual loss of "
                 f"{cached_before - cached_after} cached tokens"))
    return Result(provider, tokname, len(tb), len(ta), cp,
                  None if identical else cp, n, seg, surv_frac, surv_tokens,
                  model, floor, floor_truncated, cached_before, cached_after,
                  cached_before - cached_after, recovery, verdict, bd, notes)


def _print_models() -> int:
    for prov, r in sorted(PROVIDERS.items()):
        print(f"{prov}  (max_breakpoints={r['max_breakpoints']}, "
              f"verified {LAST_VERIFIED})")
        for m, f in sorted(r["models"].items(), key=lambda kv: (kv[1], kv[0])):
            print(f"    {m:18} floor {f:>5} tokens")
    return 0


def main() -> int:
    # Handled before argparse so the table is reachable without the required
    # measurement arguments -- a stranger's first question is "what do I pass".
    if "--list-models" in sys.argv[1:]:
        return _print_models()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--before", required=True, help="path to the prompt before the change")
    ap.add_argument("--after", required=True, help="path to the prompt after the change")
    ap.add_argument("--provider", default="anthropic", choices=sorted(PROVIDERS))
    ap.add_argument("--tokenizer", required=True,
                    help="'hf:<model-id>' or 'tiktoken:<encoding>'. Required and "
                         "never defaulted: it changes every number below.")
    ap.add_argument("--breakpoint-tokens", required=True,
                    help="comma-separated TOKEN offsets of the cache breakpoints, "
                         "ascending. Required: the cost of a change depends "
                         "entirely on where the cached region ends, and this tool "
                         "will not assume it. e.g. --breakpoint-tokens 2048,4096")
    ap.add_argument("--model", required=True,
                    help="EXACT model key, e.g. 'haiku-4.5'. Required and never "
                         "defaulted: the minimum-cacheable floor varies 8x "
                         "across a provider's line-up and a wrong floor exits 0 "
                         "on a change that destroys the entire cache. "
                         "--list-models prints the table.")
    ap.add_argument("--normalize", action="store_true",
                    help="NFC-normalise both sides before tokenising")
    ap.add_argument("--min-recovery", type=float, default=0.9,
                    help="exit 1 if recovery falls below this")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list-models", action="store_true",
                    help="print the sourced floor table and exit")
    a = ap.parse_args()

    age = (_dt.date.today() - _dt.date.fromisoformat(LAST_VERIFIED)).days
    if age > STALE_AFTER_DAYS:
        print(f"prefixguard: WARNING — provider constants last verified "
              f"{LAST_VERIFIED} ({age} days ago). Vendors change these. "
              f"Re-verify against the vendor docs before trusting a verdict.",
              file=sys.stderr)

    try:
        r = analyse(open(a.before).read(), open(a.after).read(), a.provider,
                    a.tokenizer,
                    [int(x) for x in a.breakpoint_tokens.split(",") if x.strip()],
                    a.model, a.normalize)
    except (BreakpointsUnknown, ModelUnknown) as e:
        print(f"prefixguard: REFUSING to run — {e}", file=sys.stderr)
        return 3
    except TokenizerUnavailable as e:
        print(f"prefixguard: REFUSING to run — {e}", file=sys.stderr)
        print("  This tool will not fall back to a byte diff. A byte diff is the "
              "instrument it exists to replace:\n"
              "  it is wrong in both directions (any byte inside a cached block "
              "kills the whole block, and a\n"
              "  change past the last breakpoint costs nothing).", file=sys.stderr)
        return 3

    if a.json:
        print(json.dumps(asdict(r), indent=1))
    else:
        print(f"  provider           {r.provider}   tokenizer {r.tokenizer}")
        print(f"  tokens             {r.tokens_before} -> {r.tokens_after}, "
              f"common prefix {r.common_prefix_tokens}")
        print(f"  breakpoints        {r.n_breakpoints}, first difference in "
              f"segment {r.segment_of_first_diff}")
        print(f"  survives naively   {r.surviving_fraction_naive:.0%} "
              f"({r.surviving_tokens_naive} tokens)")
        print(f"  model              {r.model}")
        print(f"  floor              {r.min_cacheable_tokens} tokens"
              + ("  <-- TRUNCATES TO ZERO" if r.floor_truncated else ""))
        print(f"  cached tokens      {r.cached_tokens_before} -> "
              f"{r.cached_tokens_after}   (lost {r.cached_tokens_lost})")
        print(f"  RECOVERY           {r.recovery:.1%}")
        print(f"  verdict            {r.verdict}")
        print(f"  a byte diff        {r.byte_diff_would_say}")
    return 0 if r.recovery >= a.min_recovery else 1


if __name__ == "__main__":
    raise SystemExit(main())
