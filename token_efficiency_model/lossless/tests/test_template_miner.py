"""Cross-run template mining (CR2) — Drain3 boundary detection + lossless split.
Requires drain3 (skips gracefully if missing). Deterministic; no network."""
from __future__ import annotations

import uuid

import pytest

pytest.importorskip("drain3")

from token_efficiency_model.lossless.template_miner import PromptTemplateMiner

STATIC = ("You are the nightly portfolio analyst. Follow the standing instructions: "
          "review the fact sheet, compare against sector benchmarks, and produce a "
          "verdict with confidence. " * 8)


def _prompt(run_date: str, ticker: str) -> str:
    # volatile tokens EARLY — the classic cache-busting layout
    return f"RUN {run_date} TICKER {ticker} :: {STATIC}"


def test_boundary_found_after_two_runs():
    m = PromptTemplateMiner()
    k = f"k-{uuid.uuid4().hex[:8]}"
    t1 = _prompt("2026-07-01", "AAPL")
    assert m.observe_boundary(k, t1) == len(t1), "first sighting: not recurring yet"
    t2 = _prompt("2026-07-02", "MSFT")
    b = m.observe_boundary(k, t2)
    assert 0 < b < len(t2), "second run: volatile slot detected"
    assert b == t2.index("2026-07-02"), "boundary = start of first volatile token"
    # the split is byte-lossless by construction
    assert t2[:b] + t2[b:] == t2


def test_fully_stable_template_never_splits():
    m = PromptTemplateMiner()
    k = f"k-{uuid.uuid4().hex[:8]}"
    for _ in range(3):
        b = m.observe_boundary(k, STATIC)
    assert b == len(STATIC), "no volatile slot → boundary = full length (no split)"


def test_engine_split_flag_gated(monkeypatch):
    from token_efficiency_model.lossless import engine
    from token_efficiency_model.lossless.router import BrevitasRouter

    def _run(flag: bool) -> dict:
        # split is ON by default (byte-verified vs the live API); "0" is the kill-switch
        monkeypatch.setenv("BREVITAS_TEMPLATE_SPLIT", "1" if flag else "0")
        router = BrevitasRouter(provider="anthropic")
        sid = f"s-{uuid.uuid4().hex[:8]}"
        meta = {}
        for day in ("01", "02", "03"):
            body = {"model": "claude-sonnet-4-5",
                    "system": _prompt(f"2026-07-{day}", "AAPL"),
                    "messages": [{"role": "user", "content": "verdict?"}]}
            meta = engine.optimize_request(body, "anthropic", router, sid)
        return body, meta

    body, meta = _run(flag=False)
    assert "template_volatile_at" in meta, "advisory always exposed once recurring"
    assert isinstance(body["system"], (str, list))
    if isinstance(body["system"], list):
        # apply_anthropic_cache may blockify for markers, but no CR2 split of the text
        texts = [b.get("text", "") for b in body["system"]]
        assert len(texts) == 1

    body, meta = _run(flag=True)
    assert isinstance(body["system"], list) and len(body["system"]) >= 2
    joined = "".join(b.get("text", "") for b in body["system"])
    assert joined == _prompt("2026-07-03", "AAPL"), "split must be byte-lossless"


def test_system_block_list_gets_per_block_breakpoint():
    from token_efficiency_model.lossless.provider_cache import apply_anthropic_cache
    stable = "stable instructions " * 800            # >1024 tokens on its own
    body = {"model": "claude-sonnet-4-5",
            "system": [{"type": "text", "text": stable},
                       {"type": "text", "text": "volatile: run 2026-07-01"}],
            "messages": [{"role": "user", "content": "q"}]}
    plan = apply_anthropic_cache(body)
    assert "system_block[0]" in plan.positions, \
        "stable leading block must carry its own breakpoint"
    assert "cache_control" in body["system"][0]
