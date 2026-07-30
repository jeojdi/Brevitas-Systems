"""Fixture-driven tests for scripts/analysis/token_basis_check.py.

No network, no ANTHROPIC_API_KEY, no provider call anywhere in this file. The
count_tokens boundary is exercised through a fake counter so the classifier's
behaviour is pinned by arithmetic rather than by a live provider response.

Run: ./.venv/bin/python -m pytest tests/test_token_basis_check.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "analysis" / "token_basis_check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("token_basis_check", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tbc = _load_module()


# ── fixtures ─────────────────────────────────────────────────────────────────

def make_row_dict(request_id: str, *, baseline: int, receipt: int,
                  net_savings: float | None = 1.0, model: str = "claude-opus-5",
                  text: str = "hello") -> dict:
    row = {
        "request_id": request_id,
        "provider": "anthropic",
        "model": model,
        "baseline_tokens": baseline,
        "receipt_input_tokens": receipt,
        "body": {"messages": [{"role": "user", "content": text}]},
    }
    if net_savings is not None:
        row["net_savings_usd"] = net_savings
    return row


def make_row(request_id: str, **kwargs) -> "tbc.Row":
    return tbc.parse_row(make_row_dict(request_id, **kwargs), line_number=1)


class FakeCounter:
    """Stands in for TokenCounter. Returns a scripted count, never a network call."""

    def __init__(self, by_request_id: dict[str, int | Exception]):
        self._by_request_id = by_request_id
        self.calls: list[str] = []

    def count(self, row) -> int:
        self.calls.append(row.request_id)
        value = self._by_request_id[row.request_id]
        if isinstance(value, Exception):
            raise value
        return value

    def flush(self) -> None:  # pragma: no cover - parity with TokenCounter
        pass


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


# ── tokens_agree ─────────────────────────────────────────────────────────────

def test_tokens_agree_is_symmetric_and_uses_the_larger_side():
    assert tbc.tokens_agree(1000, 1005, rel_tol=0.02, abs_tol=10)
    assert tbc.tokens_agree(1005, 1000, rel_tol=0.02, abs_tol=10)
    # 3% apart on a 1000-token count exceeds a 2% relative tolerance.
    assert not tbc.tokens_agree(1000, 1030, rel_tol=0.02, abs_tol=10)
    # Small counts fall back to the absolute floor.
    assert tbc.tokens_agree(20, 28, rel_tol=0.02, abs_tol=10)


def test_tokens_agree_rejects_negative_counts():
    with pytest.raises(ValueError):
        tbc.tokens_agree(-1, 10)


# ── classify: the three required classes ─────────────────────────────────────

def test_classify_parsing_bug_when_count_tokens_backs_the_local_baseline():
    # Provider says 1000; we stored a receipt of 4200. Our receipt read is wrong.
    verdict, reason = tbc.classify(1000, 4200, 1002)
    assert verdict == tbc.PARSING_BUG
    assert reason == tbc.REASON_LOCAL_ONLY


def test_classify_tokenizer_bias_when_count_tokens_backs_the_receipt():
    # Provider says 4180; the receipt said 4200 and the local tokenizer said 1000.
    verdict, reason = tbc.classify(1000, 4200, 4180)
    assert verdict == tbc.TOKENIZER_BIAS
    assert reason == tbc.REASON_RECEIPT_ONLY


def test_classify_unknown_when_count_tokens_backs_neither():
    verdict, reason = tbc.classify(1000, 4200, 2500)
    assert verdict == tbc.UNKNOWN
    assert reason == tbc.REASON_NEITHER


def test_classify_unknown_when_local_and_receipt_already_agree():
    # No discrepancy to explain — this row should not have been in the sample.
    verdict, reason = tbc.classify(1000, 1004, 1002)
    assert verdict == tbc.UNKNOWN
    assert reason == tbc.REASON_BOTH


def test_classify_respects_custom_tolerances():
    # Inside a 2% relative window this is TOKENIZER_BIAS...
    assert tbc.classify(1000, 4200, 4180)[0] == tbc.TOKENIZER_BIAS
    # ...and with a zero tolerance nothing matches, so it degrades to UNKNOWN
    # rather than picking a side.
    assert tbc.classify(1000, 4200, 4180, rel_tol=0.0, abs_tol=0) == (
        tbc.UNKNOWN, tbc.REASON_NEITHER)


# ── row parsing ──────────────────────────────────────────────────────────────

def test_parse_row_sums_the_receipt_split_like_token_receipt():
    row = tbc.parse_row({
        "request_id": "req-split",
        "model": "claude-opus-5",
        "baseline_tokens": 100,
        "fresh_input_tokens": 300,
        "cached_input_tokens": 900,
        "cache_write_tokens": 50,
        "body": {"messages": [{"role": "user", "content": "hi"}]},
    }, line_number=7)
    assert row.receipt_input_tokens == 1250


@pytest.mark.parametrize("mutate, needle", [
    (lambda r: r.pop("request_id"), "request_id"),
    (lambda r: r.pop("model"), "model"),
    (lambda r: r.pop("baseline_tokens"), "baseline_tokens"),
    (lambda r: r.pop("receipt_input_tokens"), "receipt input tokens"),
    (lambda r: r.__setitem__("body", {"messages": []}), "messages"),
    (lambda r: r.__setitem__("provider", "openai"), "count_tokens"),
    (lambda r: r.__setitem__("baseline_tokens", -5), ">= 0"),
])
def test_parse_row_rejects_untrustworthy_rows(mutate, needle):
    row = make_row_dict("req-1", baseline=100, receipt=400)
    mutate(row)
    with pytest.raises(tbc.RowError) as exc:
        tbc.parse_row(row, line_number=3)
    assert needle in str(exc.value)


def test_load_rows_rejects_duplicate_request_ids(tmp_path):
    path = write_jsonl(tmp_path / "dupes.jsonl", [
        make_row_dict("req-1", baseline=100, receipt=400),
        make_row_dict("req-1", baseline=100, receipt=400),
    ])
    rows, rejected = tbc.load_rows(str(path))
    assert len(rows) == 1
    assert any("duplicate request_id" in line for line in rejected)


def test_request_kwargs_omit_absent_system_and_tools():
    row = make_row("req-1", baseline=100, receipt=400)
    assert set(row.request_kwargs()) == {"model", "messages"}

    with_extras = tbc.parse_row({
        "request_id": "req-2",
        "model": "claude-opus-5",
        "baseline_tokens": 100,
        "receipt_input_tokens": 400,
        "body": {
            "messages": [{"role": "user", "content": "hi"}],
            "system": "be terse",
            "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
        },
    }, line_number=1)
    assert set(with_extras.request_kwargs()) == {"model", "messages", "system", "tools"}


# ── check_rows / summarize ───────────────────────────────────────────────────

def test_check_rows_classifies_each_class_and_isolates_errors():
    rows = [
        make_row("req-parse", baseline=1000, receipt=4200),
        make_row("req-bias", baseline=1000, receipt=4200),
        make_row("req-unknown", baseline=1000, receipt=4200),
        make_row("req-error", baseline=1000, receipt=4200),
    ]
    counter = FakeCounter({
        "req-parse": 1002,
        "req-bias": 4180,
        "req-unknown": 2500,
        "req-error": RuntimeError("rate limited"),
    })
    results = tbc.check_rows(rows, counter, rel_tol=0.02, abs_tol=10,
                             cross_check_local=False)
    verdicts = {r.request_id: r.verdict for r in results}
    assert verdicts == {
        "req-parse": tbc.PARSING_BUG,
        "req-bias": tbc.TOKENIZER_BIAS,
        "req-unknown": tbc.UNKNOWN,
        "req-error": None,
    }
    errored = next(r for r in results if r.request_id == "req-error")
    # An uncounted row stays uncounted: no substituted or estimated value.
    assert errored.counted_tokens is None
    assert "rate limited" in errored.error
    assert not errored.classified


def test_summarize_reports_counts_and_net_savings_share():
    rows = [
        make_row("req-parse", baseline=1000, receipt=4200, net_savings=1.0),
        make_row("req-bias-a", baseline=1000, receipt=4200, net_savings=2.0),
        make_row("req-bias-b", baseline=1000, receipt=4200, net_savings=1.0),
    ]
    counter = FakeCounter({"req-parse": 1002, "req-bias-a": 4180, "req-bias-b": 4180})
    results = tbc.check_rows(rows, counter, rel_tol=0.02, abs_tol=10,
                             cross_check_local=False)
    summary = tbc.summarize(results, input_rows=3, rejected_rows=0,
                            rel_tol=0.02, abs_tol=10, sampled_subset=False)

    assert summary.counts[tbc.PARSING_BUG] == 1
    assert summary.counts[tbc.TOKENIZER_BIAS] == 2
    assert summary.counts[tbc.UNKNOWN] == 0
    assert summary.classified_rows == 3
    assert summary.net_savings_total_usd == 4.0
    assert summary.net_savings_share[tbc.PARSING_BUG] == pytest.approx(0.25)
    assert summary.net_savings_share[tbc.TOKENIZER_BIAS] == pytest.approx(0.75)
    assert summary.net_savings_blocked is None
    # All three sampled rows are above the 3x quarantine threshold.
    assert summary.over_quarantine_threshold == 3


def test_summarize_blocks_the_savings_share_when_a_row_lacks_net_savings():
    rows = [
        make_row("req-parse", baseline=1000, receipt=4200, net_savings=1.0),
        make_row("req-bias", baseline=1000, receipt=4200, net_savings=None),
    ]
    counter = FakeCounter({"req-parse": 1002, "req-bias": 4180})
    results = tbc.check_rows(rows, counter, rel_tol=0.02, abs_tol=10,
                             cross_check_local=False)
    summary = tbc.summarize(results, input_rows=2, rejected_rows=0,
                            rel_tol=0.02, abs_tol=10, sampled_subset=False)

    # Counts still stand; the dollar share refuses rather than guessing.
    assert summary.counts[tbc.PARSING_BUG] == 1
    assert summary.counts[tbc.TOKENIZER_BIAS] == 1
    assert summary.net_savings_share is None
    assert summary.net_savings_blocked.startswith("BLOCKED")


def test_summarize_flags_a_body_that_does_not_match_the_stored_baseline():
    row = make_row("req-mismatch", baseline=1000, receipt=4200)
    counter = FakeCounter({"req-mismatch": 4180})
    results = tbc.check_rows([row], counter, rel_tol=0.02, abs_tol=10,
                             cross_check_local=False)
    # A one-word body cannot really be 1000 local tokens; simulate the local
    # re-count disagreeing with what the row claims.
    results[0].recomputed_local_tokens = 4
    summary = tbc.summarize(results, input_rows=1, rejected_rows=0,
                            rel_tol=0.02, abs_tol=10, sampled_subset=False)
    assert summary.reconstruction_mismatches
    assert "req-mismatch" in summary.reconstruction_mismatches[0]


def test_render_summary_marks_a_sampled_subset_as_non_extrapolated():
    rows = [make_row("req-bias", baseline=1000, receipt=4200, net_savings=2.0)]
    counter = FakeCounter({"req-bias": 4180})
    results = tbc.check_rows(rows, counter, rel_tol=0.02, abs_tol=10,
                             cross_check_local=False)
    summary = tbc.summarize(results, input_rows=500, rejected_rows=0,
                            rel_tol=0.02, abs_tol=10, sampled_subset=True)
    report = tbc.render_summary(summary, [], [])
    assert "SUBSET of the input set" in report
    assert "does not" in report and "extrapolate" in report


# ── sampling ─────────────────────────────────────────────────────────────────

def test_select_sample_is_deterministic_and_bounded():
    rows = [make_row(f"req-{i:03d}", baseline=100, receipt=400) for i in range(40)]
    first = tbc.select_sample(rows, limit=10, seed=7)
    second = tbc.select_sample(rows, limit=10, seed=7)
    assert [r.request_id for r in first] == [r.request_id for r in second]
    assert len(first) == 10
    # limit 0 or a limit past the row count means "all rows".
    assert len(tbc.select_sample(rows, limit=0, seed=7)) == 40
    assert len(tbc.select_sample(rows, limit=999, seed=7)) == 40


# ── refusals ─────────────────────────────────────────────────────────────────

def test_main_refuses_without_an_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = write_jsonl(tmp_path / "rows.jsonl",
                       [make_row_dict("req-1", baseline=1000, receipt=4200)])
    code = tbc.main(["--input", str(path)])
    assert code == tbc.EXIT_REFUSED
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_main_refuses_on_an_empty_input_set(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    # Client construction is stubbed: this test must never reach the network.
    monkeypatch.setattr(tbc, "build_client", lambda: object())
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    code = tbc.main(["--input", str(empty)])
    err = capsys.readouterr().err
    assert code == tbc.EXIT_REFUSED
    assert "zero usable rows" in err
    assert "zero samples" in err


def test_main_refuses_when_every_count_tokens_call_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setattr(tbc, "build_client", lambda: object())
    monkeypatch.setattr(
        tbc, "TokenCounter",
        lambda client, cache_path=None: FakeCounter({
            "req-1": RuntimeError("boom"), "req-2": RuntimeError("boom")}),
    )
    path = write_jsonl(tmp_path / "rows.jsonl", [
        make_row_dict("req-1", baseline=1000, receipt=4200),
        make_row_dict("req-2", baseline=1000, receipt=4200),
    ])
    code = tbc.main(["--input", str(path)])
    err = capsys.readouterr().err
    assert code == tbc.EXIT_REFUSED
    assert "zero rows were classified" in err


def test_main_refuses_on_malformed_rows_unless_explicitly_allowed(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setattr(tbc, "build_client", lambda: object())
    path = tmp_path / "rows.jsonl"
    good = make_row_dict("req-good", baseline=1000, receipt=4200)
    bad = make_row_dict("req-bad", baseline=1000, receipt=4200)
    bad.pop("model")
    write_jsonl(path, [good, bad])

    assert tbc.main(["--input", str(path)]) == tbc.EXIT_REFUSED
    assert "unusable" in capsys.readouterr().err

    monkeypatch.setattr(
        tbc, "TokenCounter",
        lambda client, cache_path=None: FakeCounter({"req-good": 1002}),
    )
    code = tbc.main(["--input", str(path), "--allow-invalid-rows"])
    out = capsys.readouterr().out
    assert code == tbc.EXIT_OK
    assert "PARSING_BUG" in out


def test_main_emits_a_summary_and_json_for_a_clean_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setattr(tbc, "build_client", lambda: object())
    monkeypatch.setattr(
        tbc, "TokenCounter",
        lambda client, cache_path=None: FakeCounter({"req-1": 1002, "req-2": 4180}),
    )
    path = write_jsonl(tmp_path / "rows.jsonl", [
        make_row_dict("req-1", baseline=1000, receipt=4200, net_savings=1.0),
        make_row_dict("req-2", baseline=1000, receipt=4200, net_savings=3.0),
    ])
    out_path = tmp_path / "results.json"
    code = tbc.main(["--input", str(path), "--json-out", str(out_path),
                     "--no-local-cross-check"])
    assert code == tbc.EXIT_OK
    capsys.readouterr()

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["summary"]["counts"] == {
        tbc.PARSING_BUG: 1, tbc.TOKENIZER_BIAS: 1, tbc.UNKNOWN: 0}
    assert payload["summary"]["net_savings_share"][tbc.TOKENIZER_BIAS] == pytest.approx(0.75)
    assert {r["request_id"] for r in payload["results"]} == {"req-1", "req-2"}


def test_token_counter_serves_repeat_bodies_from_cache(tmp_path):
    class OneShotClient:
        def __init__(self):
            self.calls = 0
            self.messages = self

        def count_tokens(self, **kwargs):
            self.calls += 1
            return type("Response", (), {"input_tokens": 1234})()

    client = OneShotClient()
    cache_path = tmp_path / "cache.json"
    counter = tbc.TokenCounter(client, cache_path)
    row = make_row("req-1", baseline=1000, receipt=4200)

    assert counter.count(row) == 1234
    assert counter.count(row) == 1234
    assert client.calls == 1

    counter.flush()
    reloaded = tbc.TokenCounter(OneShotClient(), cache_path)
    assert reloaded.count(row) == 1234
