"""How hard can arm B be tuned on independent-document work?

Under-tuning arm B is false-positive item 1, and it is the easiest way to manufacture a win for
arm C. On a wide batch workload the tuning question is specific: a competent engineer puts the
shared instructions, schema and examples in a cacheable prefix and sends each document as a
short suffix. If the provider cache serves that prefix across *separate* requests, arm B is
strong on wide work. If it only ever serves within one growing conversation, arm B is
structurally weak there and arm C's margin on family 2 is an artifact of the provider's
scoping rather than a property of the reuse idea.

This probe answers it by measurement, in three configurations:

    fresh_shared_prefix   a new session per document, identical large system prefix
    one_session_append    a single conversation that accumulates every document
    fresh_no_prefix       a new session per document with no shared prefix (floor)

Run: python3 -m experiments.reuse.harness.probe_armb_tuning
"""

from __future__ import annotations

import json

from .model import call, new_session_id

MODEL = "claude-haiku-4-5-20251001"
CWD = "/private/tmp"
N_DOCS = 4

SCHEMA_PREFIX = "\n".join(
    [
        "You extract structured fields from invoice records.",
        "",
        "FIELD SPECIFICATION (apply exactly; do not infer beyond the text):",
    ]
    + [
        f"  field_{i:03d}: {name} - {desc}"
        for i, (name, desc) in enumerate(
            [
                ("vendor_name", "the legal entity issuing the invoice, verbatim"),
                ("invoice_total", "integer minor units, no separators"),
                ("line_item_count", "number of distinct line items"),
                ("currency_code", "ISO 4217 three letter code"),
                ("payment_terms_days", "integer days, 0 if due on receipt"),
            ]
            * 40
        )
    ]
    + [
        "",
        "OUTPUT FORMAT: one line, JSON object, keys in the order above, no whitespace.",
        "Emit nothing else.",
    ]
)


def doc(i: int) -> str:
    return (
        f"INVOICE {i:05d}\nVendor: Northgate Supply Company\n"
        f"Total: {14200 + i * 13} GBP\nLine items: {3 + i % 4}\nTerms: net {15 + (i % 3) * 15}\n"
    )


def run_config(name: str, *, fresh: bool, prefix: bool) -> dict:
    turns = []
    session = new_session_id()
    for i in range(N_DOCS):
        body = (SCHEMA_PREFIX + "\n\n" if prefix else "") + "RECORD TO EXTRACT:\n" + doc(i)
        if fresh:
            result = call(body, model=MODEL, arm="B", cwd=CWD, session_id=new_session_id())
        else:
            result = call(
                body, model=MODEL, arm="B", cwd=CWD, session_id=session, resume=(i > 0)
            )
        turns.append(
            {
                "i": i,
                "input": result.input_tokens,
                "creation": result.cache_creation_tokens,
                "read": result.cache_read_tokens,
                "output": result.output_tokens,
                "read_fraction": round(result.cache_read_fraction, 4),
            }
        )
        print(f"  {name}[{i}]: {json.dumps(turns[-1])}")

    later = turns[1:]
    billable = sum(t["input"] + t["creation"] + t["read"] for t in later)
    return {
        "config": name,
        "turns": turns,
        "read_fraction_after_first": (sum(t["read"] for t in later) / billable) if billable else 0.0,
        "total_billable_input": sum(t["input"] + t["creation"] + t["read"] for t in turns),
        "total_output": sum(t["output"] for t in turns),
    }


def main() -> int:
    print("config: fresh session per document, identical large shared prefix")
    fresh_prefix = run_config("fresh_shared_prefix", fresh=True, prefix=True)
    print("config: one accumulating session")
    one_session = run_config("one_session_append", fresh=False, prefix=True)
    print("config: fresh session per document, no shared prefix (floor)")
    fresh_none = run_config("fresh_no_prefix", fresh=True, prefix=False)

    results = [fresh_prefix, one_session, fresh_none]
    print()
    for r in results:
        print(
            f"{r['config']:22s} read_fraction_after_first={r['read_fraction_after_first']:.3f} "
            f"billable_input={r['total_billable_input']:7d} output={r['total_output']:6d}"
        )

    best = max(results, key=lambda r: -r["total_billable_input"])
    print(f"\ncheapest input configuration: {best['config']}")
    print(
        "cross-session prefix caching works"
        if fresh_prefix["read_fraction_after_first"] > 0.3
        else "cross-session prefix caching does NOT serve this shape"
    )

    with open("experiments/reuse/pilot/armb_tuning.json", "w", encoding="utf-8") as handle:
        json.dump({"results": results, "chosen": best["config"]}, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
