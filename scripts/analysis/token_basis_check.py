#!/usr/bin/env python3
"""Settle Q1: are >3x receipt/baseline rows a receipt-PARSING bug or tokenizer bias?

BILLING_CORRECTNESS_PLAN.md Q1 quarantines rows where the provider receipt's
input tokens exceed 3x the local tokenizer baseline. That quarantine moves
roughly a third of revenue, and nobody has established *why* the two disagree.
There are exactly two candidate explanations, and they have opposite billing
consequences:

  PARSING_BUG      we mis-read the provider receipt (our number is wrong)
  TOKENIZER_BIAS   the provider really does charge that many tokens; our local
                   tokenizer under-counts (the receipt is right, the quarantine
                   is destroying real revenue)

This harness resolves them against a third, independent witness: Anthropic's
`POST /v1/messages/count_tokens`, which tokenizes a body with the *provider's*
tokenizer without running inference. For each sampled row it compares three
quantities and classifies:

  local baseline   the local-tokenizer count recorded on the usage row
  receipt input    fresh + cached + cache-write input tokens from the receipt
  count_tokens     the provider's own count of the reconstructed body

  count_tokens agrees with local, disagrees with receipt   -> PARSING_BUG
  count_tokens agrees with receipt, disagrees with local   -> TOKENIZER_BIAS
  anything else                                            -> UNKNOWN

Design rules this harness holds to, because its output decides invoices:

  * It never invents a number. A row whose count_tokens call fails is an ERROR
    row, not a guess; a class with no rows reports zero, not an estimate.
  * It never emits a verdict from zero samples. Missing credentials, an empty
    input set, or an all-errors run each exit non-zero with a clear message.
  * It never rewrites a model ID. Whatever the row stored is what gets sent; a
    404 surfaces as an ERROR row rather than being silently "corrected".
  * When the run samples a subset, the summary says so and does not extrapolate
    the sample's savings share to the population.

INPUT
-----
JSONL, one object per line (`--input FILE`, or `-` for stdin). Per row:

  request_id            str, required   identifies the usage_log row
  model                 str, required   model ID exactly as stored on the row
  provider              str, optional   must be "anthropic" if present
  baseline_tokens       int, required   usage_log.baseline_tokens (local basis)
  receipt_input_tokens  int             provider receipt input tokens; or supply
                                        fresh_input_tokens + cached_input_tokens
                                        + cache_write_tokens and they are summed
                                        exactly as TokenReceipt.input_tokens does
  net_savings_usd       float, optional required for the savings-share summary
  body                  obj, required   the request AS SENT to the provider:
                                          messages (required, non-empty)
                                          system   (optional, str or blocks)
                                          tools    (optional, list)
                                        `messages`/`system`/`tools` may also be
                                        given at the top level instead.

WHY THE BODY MUST BE SUPPLIED, AND CANNOT BE READ FROM THE DATABASE
-------------------------------------------------------------------
There is no prompt content in the schema to reconstruct from. `usage_log`
carries token counts only; `UsageReportRequest.usage_raw` is documented as
"parsed then discarded; never persisted" (api/server.py), and the sole place a
request body is retained -- `warm_prefixes.payload_ciphertext` -- is encrypted
and is a cache prefix rather than a sent request. So the bodies have to be
reconstructed upstream (from client-side logs, a replay capture, or a
purpose-built instrumented run) and handed to this harness. It deliberately has
no `--sqlite` mode: a mode that silently produced zero usable bodies would look
like a clean run.

USAGE
-----
    export ANTHROPIC_API_KEY=...
    ./.venv/bin/python scripts/analysis/token_basis_check.py \
        --input samples.jsonl --limit 50 --json-out results.json

Exit codes: 0 = summary emitted from at least one classified row.
            2 = refused (no key, no SDK, bad/empty input, zero classified rows).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

EXIT_OK = 0
EXIT_REFUSED = 2

PARSING_BUG = "PARSING_BUG"
TOKENIZER_BIAS = "TOKENIZER_BIAS"
UNKNOWN = "UNKNOWN"
CLASSES = (PARSING_BUG, TOKENIZER_BIAS, UNKNOWN)

# Reasons are recorded alongside the verdict so an UNKNOWN is never opaque.
REASON_LOCAL_ONLY = "counted_matches_local_only"
REASON_RECEIPT_ONLY = "counted_matches_receipt_only"
REASON_BOTH = "counted_matches_both_no_discrepancy"
REASON_NEITHER = "counted_matches_neither"

# Agreement tolerance. count_tokens counts the whole serialized request, so a
# few tokens of envelope overhead are expected even on a perfect match; the
# discrepancy under investigation is a >3x gap, orders of magnitude larger than
# these tolerances. Both are CLI-overridable and are echoed into the summary so
# a reader can tell exactly what "agrees" meant for a given run.
DEFAULT_REL_TOL = 0.02
DEFAULT_ABS_TOL = 10
DEFAULT_LIMIT = 50
DEFAULT_SEED = 0

# The quarantine rule under investigation (BILLING_CORRECTNESS_PLAN.md Q1).
QUARANTINE_RATIO = 3.0


class RowError(ValueError):
    """A row that cannot be trusted as harness input."""


class Refusal(Exception):
    """A precondition failure that must stop the run without a verdict."""


@dataclass(frozen=True)
class Row:
    request_id: str
    model: str
    baseline_tokens: int
    receipt_input_tokens: int
    messages: list
    system: Any = None
    tools: Any = None
    net_savings_usd: float | None = None
    provider: str = ""
    line_number: int = 0

    def request_kwargs(self) -> dict:
        """Exactly what gets handed to messages.count_tokens for this row."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": self.messages}
        if self.system is not None:
            kwargs["system"] = self.system
        if self.tools is not None:
            kwargs["tools"] = self.tools
        return kwargs

    def body_fingerprint(self) -> str:
        payload = json.dumps(self.request_kwargs(), sort_keys=True,
                             ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Result:
    request_id: str
    model: str
    local_tokens: int
    receipt_input_tokens: int
    counted_tokens: int | None = None
    verdict: str | None = None
    reason: str | None = None
    error: str | None = None
    net_savings_usd: float | None = None
    recomputed_local_tokens: int | None = None
    receipt_over_local_ratio: float | None = None

    @property
    def classified(self) -> bool:
        return self.verdict is not None


# ── agreement + classification (pure; unit-tested with fixtures, no network) ──

def tokens_agree(left: int, right: int, *, rel_tol: float = DEFAULT_REL_TOL,
                 abs_tol: int = DEFAULT_ABS_TOL) -> bool:
    """True when two token counts are the same number for our purposes.

    Symmetric, and relative to the larger of the two so that the tolerance does
    not quietly widen as counts grow apart.
    """
    if left < 0 or right < 0:
        raise ValueError("token counts cannot be negative")
    delta = abs(left - right)
    return delta <= max(abs_tol, rel_tol * max(left, right))


def classify(local_tokens: int, receipt_input_tokens: int, counted_tokens: int, *,
             rel_tol: float = DEFAULT_REL_TOL,
             abs_tol: int = DEFAULT_ABS_TOL) -> tuple[str, str]:
    """Return (verdict, reason) for one row's three token counts.

    count_tokens is the arbiter: whichever of the two stored numbers it lands on
    is the one telling the truth about this request.
    """
    matches_local = tokens_agree(counted_tokens, local_tokens,
                                 rel_tol=rel_tol, abs_tol=abs_tol)
    matches_receipt = tokens_agree(counted_tokens, receipt_input_tokens,
                                   rel_tol=rel_tol, abs_tol=abs_tol)
    if matches_local and not matches_receipt:
        # The provider tokenizer agrees with our local count, so the receipt
        # number we stored is not what the provider actually charged for.
        return PARSING_BUG, REASON_LOCAL_ONLY
    if matches_receipt and not matches_local:
        # The provider really did see that many tokens; the local tokenizer is
        # the one that is off, so quarantining this row destroys real savings.
        return TOKENIZER_BIAS, REASON_RECEIPT_ONLY
    if matches_local and matches_receipt:
        # local ~= receipt: this row has no discrepancy to explain. It should
        # not have been in the quarantine sample; surfacing it as UNKNOWN with
        # this reason is a signal about the sample, not about the row.
        return UNKNOWN, REASON_BOTH
    return UNKNOWN, REASON_NEITHER


# ── input loading ────────────────────────────────────────────────────────────

def _require_int(obj: dict, key: str, *, where: str) -> int:
    if key not in obj:
        raise RowError(f"{where}: missing required field {key!r}")
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RowError(f"{where}: {key!r} must be a number, got {type(value).__name__}")
    if isinstance(value, float) and not value.is_integer():
        raise RowError(f"{where}: {key!r} must be a whole number of tokens, got {value!r}")
    ivalue = int(value)
    if ivalue < 0:
        raise RowError(f"{where}: {key!r} must be >= 0, got {ivalue}")
    return ivalue


def _receipt_input_tokens(obj: dict, *, where: str) -> int:
    """Receipt input tokens, summed exactly as TokenReceipt.input_tokens does."""
    if "receipt_input_tokens" in obj:
        return _require_int(obj, "receipt_input_tokens", where=where)
    split_keys = ("fresh_input_tokens", "cached_input_tokens", "cache_write_tokens")
    if not any(key in obj for key in split_keys):
        raise RowError(
            f"{where}: missing receipt input tokens; supply 'receipt_input_tokens' "
            f"or the split {split_keys}"
        )
    # Absent components are genuinely zero in a receipt, not unknown.
    return sum(_require_int(obj, key, where=where) for key in split_keys if key in obj)


def parse_row(obj: Any, *, line_number: int) -> Row:
    where = f"line {line_number}"
    if not isinstance(obj, dict):
        raise RowError(f"{where}: expected a JSON object, got {type(obj).__name__}")

    request_id = obj.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise RowError(f"{where}: 'request_id' must be a non-empty string")
    where = f"line {line_number} (request_id={request_id})"

    model = obj.get("model")
    if not isinstance(model, str) or not model.strip():
        raise RowError(f"{where}: 'model' must be a non-empty string")

    provider = obj.get("provider") or ""
    if not isinstance(provider, str):
        raise RowError(f"{where}: 'provider' must be a string")
    if provider and provider.strip().lower() != "anthropic":
        # count_tokens is an Anthropic endpoint. A non-Anthropic row cannot be
        # settled here at all; pretending otherwise would fabricate a verdict.
        raise RowError(
            f"{where}: provider {provider!r} cannot be checked against Anthropic's "
            f"count_tokens endpoint; exclude it from the input set"
        )

    body = obj.get("body")
    if body is None:
        body = obj
    if not isinstance(body, dict):
        raise RowError(f"{where}: 'body' must be an object")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RowError(f"{where}: 'body.messages' must be a non-empty list")
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or "role" not in message:
            raise RowError(f"{where}: body.messages[{index}] must be an object with a 'role'")

    tools = body.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise RowError(f"{where}: 'body.tools' must be a list when present")

    net_savings = obj.get("net_savings_usd")
    if net_savings is not None:
        if isinstance(net_savings, bool) or not isinstance(net_savings, (int, float)):
            raise RowError(f"{where}: 'net_savings_usd' must be a number when present")
        net_savings = float(net_savings)

    return Row(
        request_id=request_id,
        model=model,
        baseline_tokens=_require_int(obj, "baseline_tokens", where=where),
        receipt_input_tokens=_receipt_input_tokens(obj, where=where),
        messages=messages,
        system=body.get("system"),
        tools=tools,
        net_savings_usd=net_savings,
        provider=provider,
        line_number=line_number,
    )


def load_rows(path: str) -> tuple[list[Row], list[str]]:
    """Read JSONL rows. Returns (rows, per-row rejection messages)."""
    if path == "-":
        text = sys.stdin.read()
    else:
        file_path = Path(path)
        if not file_path.is_file():
            raise Refusal(f"input file not found: {path}")
        text = file_path.read_text(encoding="utf-8")

    rows: list[Row] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            rejected.append(f"line {line_number}: invalid JSON ({exc.msg})")
            continue
        try:
            row = parse_row(obj, line_number=line_number)
        except RowError as exc:
            rejected.append(str(exc))
            continue
        if row.request_id in seen:
            # A duplicated request_id would double-count one request's savings.
            rejected.append(f"line {line_number}: duplicate request_id {row.request_id!r}")
            continue
        seen.add(row.request_id)
        rows.append(row)
    return rows, rejected


def select_sample(rows: Sequence[Row], *, limit: int, seed: int) -> list[Row]:
    """Deterministic sample, so a rerun checks the same requests."""
    if limit <= 0 or limit >= len(rows):
        return list(rows)
    ordered = sorted(rows, key=lambda row: row.request_id)
    picked = random.Random(seed).sample(range(len(ordered)), limit)
    return [ordered[index] for index in sorted(picked)]


# ── local tokenizer cross-check (optional diagnostic) ────────────────────────

def recompute_local_tokens(row: Row) -> int | None:
    """Count the reconstructed body with the SAME local tokenizer production uses.

    This is a diagnostic, not the classification basis: `baseline_tokens` as
    stored on the row is what the quarantine rule actually compares against. A
    large gap between the two means the reconstruction does not match the
    request the row describes, which invalidates that row's verdict.
    Returns None when the local tokenizer cannot be imported.
    """
    try:  # imported lazily: the harness must load without the model package
        from brevitas._compress import count_messages_tokens
    except Exception:
        return None
    try:
        return int(count_messages_tokens(row.messages))
    except Exception:
        return None


# ── provider count_tokens ────────────────────────────────────────────────────

class TokenCounter:
    """Thin wrapper over messages.count_tokens with an on-disk result cache."""

    def __init__(self, client: Any, cache_path: Path | None = None) -> None:
        self._client = client
        self._cache_path = cache_path
        self._cache: dict[str, int] = {}
        if cache_path is not None and cache_path.is_file():
            try:
                loaded = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._cache = {k: int(v) for k, v in loaded.items()
                                   if isinstance(v, (int, float))}
            except (json.JSONDecodeError, OSError, ValueError):
                self._cache = {}

    def count(self, row: Row) -> int:
        key = f"{row.model}:{row.body_fingerprint()}"
        if key in self._cache:
            return self._cache[key]
        response = self._client.messages.count_tokens(**row.request_kwargs())
        counted = int(response.input_tokens)
        self._cache[key] = counted
        return counted

    def flush(self) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, sort_keys=True, indent=2), encoding="utf-8")
        except OSError as exc:  # a cache failure must not corrupt the verdict
            print(f"warning: could not write cache {self._cache_path}: {exc}",
                  file=sys.stderr)


def build_client() -> Any:
    """Construct an Anthropic client, refusing loudly rather than half-running.

    ANTHROPIC_API_KEY is required explicitly. The SDK would also accept an
    `ant auth login` profile, but this harness decides billing, so the run must
    name the credential it used rather than picking up ambient state.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise Refusal(
            "BLOCKED: ANTHROPIC_API_KEY is not set. This harness calls Anthropic's "
            "count_tokens endpoint and has no offline mode; it will not emit a "
            "verdict without it. Set ANTHROPIC_API_KEY and rerun."
        )
    try:
        import anthropic  # imported lazily so the module loads for unit tests
    except ImportError as exc:
        raise Refusal(
            "BLOCKED: the 'anthropic' package is not installed in this "
            f"environment ({exc}). Install it (pip install anthropic) and rerun."
        ) from exc
    return anthropic.Anthropic(api_key=api_key)


def check_rows(rows: Sequence[Row], counter: TokenCounter, *,
               rel_tol: float, abs_tol: int, cross_check_local: bool = True) -> list[Result]:
    results: list[Result] = []
    for row in rows:
        result = Result(
            request_id=row.request_id,
            model=row.model,
            local_tokens=row.baseline_tokens,
            receipt_input_tokens=row.receipt_input_tokens,
            net_savings_usd=row.net_savings_usd,
            receipt_over_local_ratio=(
                round(row.receipt_input_tokens / row.baseline_tokens, 4)
                if row.baseline_tokens > 0 else None
            ),
        )
        if cross_check_local:
            result.recomputed_local_tokens = recompute_local_tokens(row)
        try:
            result.counted_tokens = counter.count(row)
        except Exception as exc:  # noqa: BLE001 - any failure is an ERROR row
            # No fallback estimate: an uncounted row is uncounted, full stop.
            result.error = f"{type(exc).__name__}: {exc}"
            results.append(result)
            continue
        result.verdict, result.reason = classify(
            result.local_tokens, result.receipt_input_tokens, result.counted_tokens,
            rel_tol=rel_tol, abs_tol=abs_tol,
        )
        results.append(result)
    return results


# ── summary ──────────────────────────────────────────────────────────────────

@dataclass
class Summary:
    input_rows: int = 0
    rejected_rows: int = 0
    sampled_rows: int = 0
    sampled_subset: bool = False
    classified_rows: int = 0
    error_rows: int = 0
    rel_tol: float = DEFAULT_REL_TOL
    abs_tol: int = DEFAULT_ABS_TOL
    counts: dict[str, int] = field(default_factory=dict)
    reason_counts: dict[str, int] = field(default_factory=dict)
    median_receipt_over_local: dict[str, float | None] = field(default_factory=dict)
    over_quarantine_threshold: int = 0
    net_savings_usd: dict[str, float] = field(default_factory=dict)
    net_savings_share: dict[str, float] | None = None
    net_savings_total_usd: float | None = None
    net_savings_blocked: str | None = None
    reconstruction_mismatches: list[str] = field(default_factory=list)


def summarize(results: Sequence[Result], *, input_rows: int, rejected_rows: int,
              rel_tol: float, abs_tol: int, sampled_subset: bool,
              reconstruction_rel_tol: float = 0.10) -> Summary:
    classified = [r for r in results if r.classified]
    errors = [r for r in results if r.error is not None]

    summary = Summary(
        input_rows=input_rows,
        rejected_rows=rejected_rows,
        sampled_rows=len(results),
        sampled_subset=sampled_subset,
        classified_rows=len(classified),
        error_rows=len(errors),
        rel_tol=rel_tol,
        abs_tol=abs_tol,
        counts={name: 0 for name in CLASSES},
        net_savings_usd={name: 0.0 for name in CLASSES},
    )

    ratios: dict[str, list[float]] = {name: [] for name in CLASSES}
    for result in classified:
        summary.counts[result.verdict] = summary.counts.get(result.verdict, 0) + 1
        if result.reason:
            summary.reason_counts[result.reason] = summary.reason_counts.get(result.reason, 0) + 1
        if result.receipt_over_local_ratio is not None:
            ratios[result.verdict].append(result.receipt_over_local_ratio)
            if result.receipt_over_local_ratio > QUARANTINE_RATIO:
                summary.over_quarantine_threshold += 1
        # A reconstruction that does not re-count to roughly the row's stored
        # baseline is not the request the row describes; flag it rather than
        # letting it silently vote.
        if (result.recomputed_local_tokens is not None
                and not tokens_agree(result.recomputed_local_tokens, result.local_tokens,
                                     rel_tol=reconstruction_rel_tol, abs_tol=abs_tol)):
            summary.reconstruction_mismatches.append(
                f"{result.request_id}: stored baseline {result.local_tokens} vs "
                f"recomputed {result.recomputed_local_tokens}"
            )

    summary.median_receipt_over_local = {
        name: (round(statistics.median(values), 4) if values else None)
        for name, values in ratios.items()
    }

    missing_savings = [r.request_id for r in classified if r.net_savings_usd is None]
    if not classified:
        summary.net_savings_blocked = "no classified rows"
    elif missing_savings:
        summary.net_savings_blocked = (
            f"BLOCKED: net-savings share needs 'net_savings_usd' on every classified "
            f"row; {len(missing_savings)} of {len(classified)} lack it "
            f"(e.g. {missing_savings[0]}). Counts above are unaffected."
        )
    else:
        for result in classified:
            summary.net_savings_usd[result.verdict] += float(result.net_savings_usd)
        total = sum(summary.net_savings_usd.values())
        summary.net_savings_total_usd = round(total, 6)
        if total == 0:
            summary.net_savings_blocked = (
                "net savings across classified rows total exactly 0.00; shares are "
                "undefined and are not reported."
            )
        else:
            summary.net_savings_share = {
                name: round(value / total, 6)
                for name, value in summary.net_savings_usd.items()
            }
        summary.net_savings_usd = {k: round(v, 6) for k, v in summary.net_savings_usd.items()}
    return summary


def render_summary(summary: Summary, rejected: Sequence[str],
                   errors: Sequence[Result]) -> str:
    lines: list[str] = []
    lines.append("token basis check — Q1: receipt-parsing bug vs tokenizer bias")
    lines.append("=" * 62)
    lines.append(f"input rows              : {summary.input_rows}")
    lines.append(f"rejected rows           : {summary.rejected_rows}")
    lines.append(f"sampled rows            : {summary.sampled_rows}"
                 + ("  (SUBSET of the input set)" if summary.sampled_subset else "  (all rows)"))
    lines.append(f"classified rows         : {summary.classified_rows}")
    lines.append(f"count_tokens errors     : {summary.error_rows}")
    lines.append(f"agreement tolerance     : rel {summary.rel_tol}, abs {summary.abs_tol} tokens")
    lines.append(f"rows over {QUARANTINE_RATIO}x quarantine : {summary.over_quarantine_threshold}"
                 " (of classified)")
    lines.append("")
    lines.append("verdicts")
    lines.append("-" * 62)
    for name in CLASSES:
        count = summary.counts.get(name, 0)
        share = (count / summary.classified_rows) if summary.classified_rows else 0.0
        median = summary.median_receipt_over_local.get(name)
        median_text = f"median receipt/local {median}" if median is not None else "median n/a"
        lines.append(f"  {name:<15} {count:>5}  ({share:6.1%})  {median_text}")
    if summary.reason_counts:
        lines.append("")
        lines.append("reasons")
        lines.append("-" * 62)
        for reason, count in sorted(summary.reason_counts.items()):
            lines.append(f"  {reason:<38} {count:>5}")

    lines.append("")
    lines.append("net savings by class")
    lines.append("-" * 62)
    if summary.net_savings_share is None:
        lines.append(f"  {summary.net_savings_blocked}")
    else:
        lines.append(f"  total classified net savings: ${summary.net_savings_total_usd}")
        for name in CLASSES:
            usd = summary.net_savings_usd.get(name, 0.0)
            lines.append(f"  {name:<15} ${usd:>12.6f}  ({summary.net_savings_share[name]:6.1%})")
        if summary.sampled_subset:
            lines.append("  NOTE: these shares describe the SAMPLED rows only. This harness "
                         "does not")
            lines.append("        extrapolate them to the full population.")

    if summary.reconstruction_mismatches:
        lines.append("")
        lines.append("reconstruction mismatches (verdicts for these rows are NOT trustworthy)")
        lines.append("-" * 62)
        for line in summary.reconstruction_mismatches[:20]:
            lines.append(f"  {line}")
        if len(summary.reconstruction_mismatches) > 20:
            lines.append(f"  ... and {len(summary.reconstruction_mismatches) - 20} more")

    if errors:
        lines.append("")
        lines.append("count_tokens errors (unclassified — no value was assumed)")
        lines.append("-" * 62)
        for result in errors[:20]:
            lines.append(f"  {result.request_id}: {result.error}")
        if len(errors) > 20:
            lines.append(f"  ... and {len(errors) - 20} more")

    if rejected:
        lines.append("")
        lines.append("rejected input rows")
        lines.append("-" * 62)
        for line in rejected[:20]:
            lines.append(f"  {line}")
        if len(rejected) > 20:
            lines.append(f"  ... and {len(rejected) - 20} more")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token_basis_check",
        description=("Classify receipt/baseline token discrepancies as PARSING_BUG, "
                     "TOKENIZER_BIAS, or UNKNOWN using Anthropic count_tokens."),
    )
    parser.add_argument("--input", required=True,
                        help="JSONL file of sample rows, or '-' for stdin")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"max rows to check (default {DEFAULT_LIMIT}; 0 = all)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="sampling seed, for a reproducible subset")
    parser.add_argument("--rel-tol", type=float, default=DEFAULT_REL_TOL,
                        help=f"relative agreement tolerance (default {DEFAULT_REL_TOL})")
    parser.add_argument("--abs-tol", type=int, default=DEFAULT_ABS_TOL,
                        help=f"absolute agreement tolerance in tokens (default {DEFAULT_ABS_TOL})")
    parser.add_argument("--allow-invalid-rows", action="store_true",
                        help="continue when some input rows are malformed (default: refuse)")
    parser.add_argument("--no-local-cross-check", action="store_true",
                        help="skip re-counting bodies with the local tokenizer")
    parser.add_argument("--cache", default="",
                        help="path to a JSON cache of count_tokens results")
    parser.add_argument("--json-out", default="",
                        help="write per-row results and the summary to this path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED


def _run(args: argparse.Namespace) -> int:
    if args.rel_tol < 0 or args.abs_tol < 0:
        raise Refusal("BLOCKED: --rel-tol and --abs-tol must be >= 0.")

    # Credentials are checked before anything else so a run that cannot possibly
    # finish fails immediately rather than after reading a large input set.
    client = build_client()

    rows, rejected = load_rows(args.input)
    if rejected and not args.allow_invalid_rows:
        detail = "\n".join(f"  {line}" for line in rejected[:20])
        extra = f"\n  ... and {len(rejected) - 20} more" if len(rejected) > 20 else ""
        raise Refusal(
            f"BLOCKED: {len(rejected)} input row(s) are unusable and this harness "
            f"will not quietly drop them:\n{detail}{extra}\n"
            f"Fix the input set, or rerun with --allow-invalid-rows to proceed "
            f"with the {len(rows)} valid row(s)."
        )
    if not rows:
        raise Refusal(
            "BLOCKED: the input set contains zero usable rows. This harness will "
            "not emit a verdict from zero samples. Supply reconstructed request "
            "bodies (see the module docstring: the schema stores no prompt content, "
            "so they must come from an upstream capture)."
        )

    sample = select_sample(rows, limit=args.limit, seed=args.seed)
    counter = TokenCounter(client, Path(args.cache) if args.cache else None)
    try:
        results = check_rows(
            sample, counter,
            rel_tol=args.rel_tol, abs_tol=args.abs_tol,
            cross_check_local=not args.no_local_cross_check,
        )
    finally:
        counter.flush()

    summary = summarize(
        results,
        input_rows=len(rows),
        rejected_rows=len(rejected),
        rel_tol=args.rel_tol,
        abs_tol=args.abs_tol,
        sampled_subset=len(sample) < len(rows),
    )
    errors = [r for r in results if r.error is not None]
    report = render_summary(summary, rejected, errors)

    if args.json_out:
        payload = {"summary": asdict(summary),
                   "results": [asdict(r) for r in results]}
        Path(args.json_out).write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    if summary.classified_rows == 0:
        print(report, file=sys.stderr)
        raise Refusal(
            "\nBLOCKED: zero rows were classified — every count_tokens call failed. "
            "No verdict is emitted from zero samples."
        )

    print(report)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
