"""Does prefixguard beat a byte diff, and can this suite actually fail?

The previous version of this file passed while the tool's differentiating logic
was dead. Three specific defects, all now covered:

  * It CRASHED on any machine with tiktoken installed. Case D assumed
    `tiktoken:o200k_base` would raise TokenizerUnavailable; with tiktoken
    present it raises BreakpointsUnknown instead, which nothing caught, so the
    process died before case E and before the PASS/FAIL banner. Every
    requirements file in the repo pins tiktoken, so the normal install was the
    broken one. Case D was ALSO a no-op: neither branch appended to FAIL.

  * THE FLOOR RULE WAS NEVER EXERCISED. `floor_truncated` requires
    0 < survivors < floor; instrumenting the old suite showed survivors of
    5000, 0, 5000, 0, 6000 against floor 1024 -- never once in the open
    interval. Deleting the rule annotated "this is the whole point of the tool"
    left the suite green. F1 below is a genuine floor case and F2 is a vacuity
    guard that proves the rule is load-bearing by disabling it and requiring the
    answer to change.

  * A prompt too short to be cacheable was reported TOTAL LOSS while the tool
    itself computed zero tokens lost -- a false positive on a free change, in a
    tool whose README says false positives are how a CI gate gets switched off.

Run: python3 test_prefixguard.py     (exit 0 pass, 1 fail)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prefixguard as pg
from prefixguard import (analyse, BreakpointsUnknown, ModelUnknown,
                         TokenizerUnavailable)

TOK = "hf:Qwen/Qwen2.5-0.5B-Instruct"
HERE = os.path.dirname(os.path.abspath(__file__))
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}: {detail}")
    if not cond:
        FAIL.append(f"{name}: {detail}")


def refuses(name: str, exc, fn, why: str) -> None:
    """A refusal test that actually asserts. The old case D did not."""
    try:
        fn()
    except exc as e:
        check(name, True, f"refused with {type(e).__name__} — {why}")
        return
    except Exception as e:                                         # noqa: BLE001
        check(name, False, f"raised {type(e).__name__} instead of "
                           f"{exc.__name__}: {e}")
        return
    check(name, False, f"did NOT refuse; it should have ({why})")


# --------------------------------------------------------------------------
# Fixtures. Token offsets are DERIVED from the tokenizer, never guessed from
# character counts -- an earlier fixture sliced at char 7000 (~token 1750) and
# the resulting "tool is wrong" reading was a fixture error, twice.
# --------------------------------------------------------------------------
try:
    from transformers import AutoTokenizer
    _tk = AutoTokenizer.from_pretrained(TOK[3:])
except Exception as e:                                             # noqa: BLE001
    print(f"CANNOT RUN: tokenizer unavailable ({type(e).__name__}: {e}).")
    print("This suite refuses to run against a byte diff for the same reason "
          "the tool does. Install transformers and warm the HF cache.")
    raise SystemExit(1)

SENT = "Follow the house style, cite every source, and never speculate. "
LONG = SENT * 1400
_ids = _tk(LONG, add_special_tokens=False).input_ids
NTOK = len(_ids)
assert NTOK > 13000, f"fixture too short ({NTOK} tokens); the floor cases need room"


def edit_at(token_idx: int, text: str = LONG) -> str:
    """Return `text` with a one-token change at approximately `token_idx`."""
    enc = _tk(text, add_special_tokens=False)
    cut = enc.token_to_chars(token_idx).start
    return text[:cut] + text[cut:].replace("house", "houses", 1)


print("=" * 78)
print(f"prefixguard suite — fixture is {NTOK} tokens, tiktoken installed: "
      f"{'yes' if _tk and __import__('importlib').util.find_spec('tiktoken') else 'no'}")
print("=" * 78)

# --------------------------------------------------------------------------
print("\nA  change PAST the last breakpoint costs nothing (byte diff flags it)")
r = analyse(LONG, LONG + "\n\nUser: what changed?", "anthropic", TOK,
            [2000, 8000], "haiku-4.5")
check("A", r.recovery == 1.0 and r.cached_tokens_lost == 0,
      f"recovery {r.recovery:.0%}, lost {r.cached_tokens_lost} tokens")

# --------------------------------------------------------------------------
print("\nC  identical prompts (control)")
r = analyse(LONG, LONG, "anthropic", TOK, [2000, 8000], "haiku-4.5")
check("C", r.recovery == 1.0 and r.floor_truncated is False,
      f"recovery {r.recovery:.0%}")

# --------------------------------------------------------------------------
print("\nD  partial: survivors ABOVE the floor -> a real fractional recovery")
r_partial = analyse(LONG, edit_at(9000), "anthropic", TOK,
                    [2000, 8000, 12000], "haiku-4.5")
check("D", (not r_partial.floor_truncated) and 0.6 < r_partial.recovery < 0.7,
      f"recovery {r_partial.recovery:.1%} = 8000/12000, floor_truncated="
      f"{r_partial.floor_truncated}, survivors {r_partial.surviving_tokens_naive} "
      f"> floor {r_partial.min_cacheable_tokens}")

# --------------------------------------------------------------------------
print("\nF1 THE FLOOR RULE: survivors > 0 but BELOW the floor -> total loss")
# haiku-4.5's floor is 4096. Breakpoints at 2000 and 8000; the edit lands at
# token ~5000, so 2000 tokens survive the breakpoint -- and 2000 < 4096, so
# nothing is cacheable. Naive (j-1)/n arithmetic says 25%.
r_floor = analyse(LONG, edit_at(5000), "anthropic", TOK,
                  [2000, 8000], "haiku-4.5")
check("F1", (r_floor.floor_truncated and r_floor.recovery == 0.0
             and 0 < r_floor.surviving_tokens_naive < r_floor.min_cacheable_tokens),
      f"survivors {r_floor.surviving_tokens_naive} < floor "
      f"{r_floor.min_cacheable_tokens} -> recovery {r_floor.recovery:.0%} "
      f"(naive said {r_floor.surviving_fraction_naive:.0%})")

# --------------------------------------------------------------------------
print("\nF2 VACUITY GUARD: with the floor disabled, F1 must give a DIFFERENT answer")
# If this passes with the floor rule deleted, F1 proves nothing. Lower the floor
# and require the verdict to move. This is the check whose absence let the
# tool's only novelty go untested.
_saved = pg.PROVIDERS["anthropic"]["models"]["haiku-4.5"]
try:
    pg.PROVIDERS["anthropic"]["models"]["haiku-4.5"] = 1
    r_nofloor = analyse(LONG, edit_at(5000), "anthropic", TOK,
                        [2000, 8000], "haiku-4.5")
finally:
    pg.PROVIDERS["anthropic"]["models"]["haiku-4.5"] = _saved
check("F2", (not r_nofloor.floor_truncated) and r_nofloor.recovery > 0.2,
      f"floor=1 gives recovery {r_nofloor.recovery:.0%} vs "
      f"{r_floor.recovery:.0%} with the real floor — the rule is load-bearing")

# --------------------------------------------------------------------------
print("\nG  a prefix that was NEVER cacheable is not a regression")
SHORT = SENT * 40                       # a few hundred tokens, under every floor
n_short = len(_tk(SHORT, add_special_tokens=False).input_ids)
r_short = analyse(SHORT, SHORT.replace("house", "houses", 1), "anthropic", TOK,
                  [n_short], "haiku-4.5")
check("G", r_short.recovery == 1.0 and "NO COST" in r_short.verdict
      and r_short.cached_tokens_lost == 0,
      f"{n_short}-token prefix < 4096 floor -> {r_short.verdict[:48]}")

# --------------------------------------------------------------------------
print("\nH  the byte-diff comparison counts real BYTES, not characters")
NON_ASCII = "日本語のシステムプロンプト。" * 400
r_b = analyse(NON_ASCII, NON_ASCII + "x", "anthropic", TOK,
              [len(_tk(NON_ASCII, add_special_tokens=False).input_ids)], "opus-5")
n_bytes = len(NON_ASCII.encode("utf-8"))
check("H", str(n_bytes) in r_b.byte_diff_would_say,
      f"reports {n_bytes} bytes (chars would be {len(NON_ASCII)}, a "
      f"{n_bytes / len(NON_ASCII):.1f}x misquote)")

# --------------------------------------------------------------------------
print("\nREFUSALS — the three things this tool will not guess")
refuses("R1", TokenizerUnavailable,
        lambda: analyse(LONG, LONG + "x", "anthropic",
                        "hf:this-model-id/does-not-exist", [2000], "opus-5"),
        "no byte-diff fallback when the tokenizer will not load")
refuses("R2", BreakpointsUnknown,
        lambda: analyse(LONG, LONG + "x", "anthropic", TOK, [], "opus-5"),
        "will not infer where the cached region ends")
refuses("R3", BreakpointsUnknown,
        lambda: analyse(LONG, LONG + "x", "anthropic", TOK,
                        [2000, NTOK + 50_000], "opus-5"),
        "will not silently relocate a breakpoint that is past the prompt end")
refuses("R4", ModelUnknown,
        lambda: analyse(LONG, LONG + "x", "anthropic", TOK, [2000], "haiku"),
        "will not guess a floor; 'haiku' is ambiguous across 2048 and 4096")
refuses("R5", ModelUnknown,
        lambda: analyse(LONG, LONG + "x", "anthropic", TOK, [2000], None),
        "will not default the floor at all")

# --------------------------------------------------------------------------
print("\nS  the constants are DERIVED from PROVIDER_SOURCE.md, not asserted")
# A mutation test found the hole this closes: reverting haiku-4.5 from 4096 to
# 2048 -- the precise historical defect -- left every other check green, because
# nothing in the suite compared the table to anything. Asserting the constants
# in the test would just move the transcription one file over, so the test PARSES
# the sourced extract that ships with the tool and diffs it.
import re as _re

_src_path = os.path.join(HERE, "PROVIDER_SOURCE.md")
_src, _prov = {}, None
for _line in open(_src_path):
    _h = _re.match(r"^##\s+(\S+)\s*$", _line)
    if _h:
        _prov = _h.group(1)
        _src[_prov] = {}
        continue
    _row = _re.match(r"^\|\s*([\w.+-]+)\s*\|\s*(\d+)\s*\|\s*$", _line)
    if _row and _prov and _row.group(1) != "model-key":
        _src[_prov][_row.group(1)] = int(_row.group(2))

check("S0", sum(len(v) for v in _src.values()) >= 24,
      f"parsed {sum(len(v) for v in _src.values())} floors from PROVIDER_SOURCE.md")
for _p in sorted(set(_src) | set(pg.PROVIDERS)):
    _code = pg.PROVIDERS.get(_p, {}).get("models", {})
    _doc = _src.get(_p, {})
    _diff = {k: (_code.get(k), _doc.get(k)) for k in set(_code) | set(_doc)
             if _code.get(k) != _doc.get(k)}
    check(f"S:{_p}", not _diff,
          "code matches the source extract" if not _diff
          else f"code vs PROVIDER_SOURCE.md disagree: {_diff}")

_stale = (__import__("datetime").date.today()
          - __import__("datetime").date.fromisoformat(pg.LAST_VERIFIED)).days
check("S:date", pg.LAST_VERIFIED in open(_src_path).read(),
      f"LAST_VERIFIED {pg.LAST_VERIFIED} appears in the source extract "
      f"({_stale} days old)")

print("\nEXIT CODES — all three documented codes must be reachable from the CLI")
os.makedirs("/tmp/pgtest", exist_ok=True)
open("/tmp/pgtest/before.txt", "w").write(LONG)
open("/tmp/pgtest/pass.txt", "w").write(LONG + "\n\nUser: what changed?")
open("/tmp/pgtest/fail.txt", "w").write(edit_at(5000))


def cli(after: str, *extra: str) -> int:
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "prefixguard.py"),
         "--before", "/tmp/pgtest/before.txt", "--after", after,
         "--provider", "anthropic", "--tokenizer", TOK,
         "--breakpoint-tokens", "2000,8000", "--model", "haiku-4.5", *extra],
        capture_output=True, text=True).returncode


check("exit0", cli("/tmp/pgtest/pass.txt") == 0, "free change -> 0")
check("exit1", cli("/tmp/pgtest/fail.txt") == 1, "cache-destroying change -> 1")
code3 = subprocess.run(
    [sys.executable, os.path.join(HERE, "prefixguard.py"),
     "--before", "/tmp/pgtest/before.txt", "--after", "/tmp/pgtest/pass.txt",
     "--provider", "anthropic", "--tokenizer", TOK,
     "--breakpoint-tokens", "2000,8000", "--model", "nonexistent-model"],
    capture_output=True, text=True).returncode
check("exit3", code3 == 3, f"unknown model -> refusal, got {code3}")

# --------------------------------------------------------------------------
print("\n" + "=" * 78)
if FAIL:
    print(f"FAIL — {len(FAIL)} check(s):")
    for f in FAIL:
        print("  *", f)
    raise SystemExit(1)
print("PASS — every check held, including:")
print(f"  the floor rule fires ({r_floor.surviving_tokens_naive} survivors < "
      f"{r_floor.min_cacheable_tokens} floor -> 0%, naive said "
      f"{r_floor.surviving_fraction_naive:.0%})")
print(f"  and disabling it changes the answer to {r_nofloor.recovery:.0%}, "
      f"so the rule is not decoration")
print("  all three documented exit codes are reachable")
print("=" * 78)
