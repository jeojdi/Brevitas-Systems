"""Can this suite fail? Every check below has a named mutation that must turn it red.

The rule this repo learned the hard way: a check that has never been observed to
fail is not evidence. So this suite covers, in order:

  * arithmetic verified against a HAND-COMPUTED expected value, not against the
    tool's own output;
  * the OpenAI subset trap -- cached_tokens is a SUBSET of prompt_tokens, and
    adding it Anthropic-style inflates the hit rate. If the adapter regresses,
    A3 catches it;
  * every refusal, asserted (a refusal test that does not assert is decoration,
    which is how prefixguard's case D shipped as a no-op);
  * all three exit codes, exercised through the CLI.

Run: python3 test_cacheledger.py     (exit 0 pass, 1 fail)
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cacheledger as cl
from cacheledger import Refuse, analyse, load

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = "/tmp/cacheledger_test"
os.makedirs(TMP, exist_ok=True)
FAIL: list[str] = []

RATES = {"default": {"input": 1.00, "read": 0.10,
                     "write_5m": 1.25, "write_1h": 2.00}}


def check(name, cond, detail):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}: {detail}")
    if not cond:
        FAIL.append(f"{name}: {detail}")


def refuses(name, fn, why):
    try:
        fn()
    except Refuse as e:
        check(name, True, f"refused — {why}  [{str(e)[:52]}...]")
        return
    except Exception as e:                                         # noqa: BLE001
        check(name, False, f"raised {type(e).__name__}, not Refuse: {e}")
        return
    check(name, False, f"did NOT refuse ({why})")


def anthropic_line(sess, ts, read, create, inp=0, out=100, tier="1h"):
    return json.dumps({
        "ts": ts, "session": sess, "model": "m",
        "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                  "cache_creation_input_tokens": create,
                  "output_tokens": out,
                  "cache_creation": {
                      "ephemeral_5m_input_tokens": create if tier == "5m" else 0,
                      "ephemeral_1h_input_tokens": create if tier == "1h" else 0}}})


print("=" * 74)
print("cacheledger suite")
print("=" * 74)

# --------------------------------------------------------------------------
print("\nA  arithmetic against a hand-computed expectation")
# One session: request 1 writes 1,000,000 at the 1h tier; requests 2-3 each read
# it back. Nothing fresh.
#   prompt tokens = 1,000,000 + 1,000,000 + 1,000,000 = 3,000,000
#   hit rate      = 2,000,000 / 3,000,000 = 0.6667
#   billed        = 1e6 * 2.00/1e6  (write_1h)   = $2.00
#                 + 2e6 * 0.10/1e6  (read)       = $0.20   -> $2.20
#   no-cache      = 3e6 * 1.00/1e6                         = $3.00
#   saving        = $0.80 = 26.667%
p = os.path.join(TMP, "a.jsonl")
with open(p, "w") as f:
    f.write(anthropic_line("s1", "2026-08-27T00:00:00Z", 0, 1_000_000) + "\n")
    f.write(anthropic_line("s1", "2026-08-27T00:00:10Z", 1_000_000, 0) + "\n")
    f.write(anthropic_line("s1", "2026-08-27T00:00:20Z", 1_000_000, 0) + "\n")
r = analyse(load([p], "anthropic", None), RATES, min_tokens=1000)
check("A1", abs(r["hit_rate"] - 2 / 3) < 1e-9,
      f"hit rate {r['hit_rate']:.6f}, hand-computed 0.666667")
check("A2", abs(r["billed_usd"] - 2.20) < 1e-9 and
      abs(r["no_cache_counterfactual_usd"] - 3.00) < 1e-9,
      f"billed ${r['billed_usd']:.4f} vs hand-computed $2.2000; "
      f"counterfactual ${r['no_cache_counterfactual_usd']:.4f} vs $3.0000")
check("A2b", abs(r["saving_pct"] - 0.8 / 3.0) < 1e-9,
      f"saving {r['saving_pct']:.4%}, hand-computed 26.6667%")
check("A2c", r["tier_1h_share_of_writes"] == 1.0,
      f"1h tier share {r['tier_1h_share_of_writes']}")

# --------------------------------------------------------------------------
print("\nA3 the OpenAI subset trap: cached_tokens is INSIDE prompt_tokens")
po = os.path.join(TMP, "o.jsonl")
with open(po, "w") as f:
    for i in range(3):
        f.write(json.dumps({
            "ts": f"2026-08-27T00:00:{i:02d}Z", "session": "s", "model": "m",
            "usage": {"prompt_tokens": 1_000_000,
                      "prompt_tokens_details": {"cached_tokens": 800_000},
                      "completion_tokens": 10}}) + "\n")
ro = analyse(load([po], "openai", None), RATES, min_tokens=1000)
# prompt tokens must be 3,000,000 -- NOT 3,000,000 + 2,400,000 cached.
check("A3", ro["prompt_tokens"] == 3_000_000 and abs(ro["hit_rate"] - 0.8) < 1e-9,
      f"prompt {ro['prompt_tokens']:,} (double-counting would give 5,400,000), "
      f"hit rate {ro['hit_rate']:.1%}")

# --------------------------------------------------------------------------
print("\nB  wasted write premium: a prefix marked and never read back")
pb = os.path.join(TMP, "b.jsonl")
with open(pb, "w") as f:                       # writes 2M, reads nothing, ever
    f.write(anthropic_line("lonely", "2026-08-27T00:00:00Z", 0, 2_000_000,
                           tier="5m") + "\n")
rb = analyse(load([pb], "anthropic", None), RATES, min_tokens=1000)
#   premium = 2e6 * (1.25 - 1.00)/1e6 = $0.50
check("B", (abs(rb["wasted_write_premium_usd"] - 0.50) < 1e-9
            and rb["sessions_writing_more_than_they_read"] == 1),
      f"${rb['wasted_write_premium_usd']:.4f} premium on "
      f"{rb['wasted_write_tokens']:,} tokens, hand-computed $0.5000")
check("B2", rb["saving_pct"] < 0,
      f"saving is NEGATIVE ({rb['saving_pct']:.1%}) — marking a never-reused "
      f"prefix costs more than not caching, which is the whole point of B")

# --------------------------------------------------------------------------
print("\nC  TTL cliff: a long gap that lost the prefix is counted as lost")
pc = os.path.join(TMP, "c.jsonl")
with open(pc, "w") as f:
    f.write(anthropic_line("s", "2026-08-27T00:00:00Z", 0, 1_000_000) + "\n")
    f.write(anthropic_line("s", "2026-08-27T00:00:30Z", 1_000_000, 0) + "\n")
    # 3 hours later: prefix gone, re-created from scratch
    f.write(anthropic_line("s", "2026-08-27T03:00:30Z", 0, 1_000_000) + "\n")
rc_ = analyse(load([pc], "anthropic", None), RATES, min_tokens=1000)
short = [c for c in rc_["ttl_cliff"] if c["gap_lo_s"] == 0][0]
long_ = [c for c in rc_["ttl_cliff"] if c["gap_lo_s"] == 7200][0]
check("C", short["loss_rate"] == 0.0 and long_["loss_rate"] == 1.0,
      f"<300s loss {short['loss_rate']:.0%}, >7200s loss {long_['loss_rate']:.0%}")

# --------------------------------------------------------------------------
print("\nREFUSALS")
refuses("R1", lambda: analyse(load([p], "anthropic", None), {}, 1000),
        "no rate card entry — prices are never guessed")
refuses("R2", lambda: analyse(load([p], "anthropic", None), RATES, 10**12),
        "too few tokens for the ratio to mean anything")
refuses("R3", lambda: analyse([], RATES, 1000), "no usable receipts")
pbad = os.path.join(TMP, "bad.jsonl")
open(pbad, "w").write("not json\n" * 50 + anthropic_line("s", None, 1, 1) + "\n")
refuses("R4", lambda: load([pbad], "anthropic", None),
        "mostly unparseable — that is not a log in this format")
pmix = os.path.join(TMP, "mix.jsonl")
open(pmix, "w").write(json.dumps({
    "usage": {"prompt_tokens": 100, "prompt_tokens_details":
              {"cached_tokens": 900}}}) + "\n")
refuses("R5", lambda: load([pmix], "openai", None),
        "cached_tokens > prompt_tokens — Anthropic data fed through --from openai")

# --------------------------------------------------------------------------
print("\nEXIT CODES")
rp = os.path.join(TMP, "rates.json")
json.dump(RATES, open(rp, "w"))
BIN = os.path.join(HERE, "cacheledger.py")


def cli(*extra, log=p):
    return subprocess.run([sys.executable, BIN, log, "--from", "anthropic",
                           "--rates", rp, "--min-tokens", "1000", *extra],
                          capture_output=True, text=True).returncode


check("exit0", cli() == 0, "a healthy ledger -> 0")
check("exit1", cli("--min-hit-rate", "0.99") == 1,
      "hit rate below the gate -> 1")
check("exit1b", cli("--max-wasted-usd", "0.01", log=pb) == 1,
      "wasted write premium over the gate -> 1")
check("exit3", subprocess.run(
    [sys.executable, BIN, p, "--from", "anthropic", "--rates",
     "/nonexistent/rates.json"], capture_output=True, text=True).returncode == 3,
      "missing rate card -> refusal, not a guess")

print("\n" + "=" * 74)
if FAIL:
    print(f"FAIL — {len(FAIL)} check(s):")
    for f_ in FAIL:
        print("  *", f_)
    raise SystemExit(1)
print("PASS — arithmetic matches hand computation, the OpenAI subset trap is")
print("       covered, every refusal asserts, and all three exit codes fire.")
print("=" * 74)
