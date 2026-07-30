"""GET /v1/audit — traffic-side optimization audit.

Honesty contract under test: every "opportunity" must be backed by stored
usage rows; anything the rows cannot prove stays "unknown", and a prospect
that already does an optimization is never told it is an opportunity.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore, _audit_metrics


def _setup(tmp_path, monkeypatch, name, raw_key="bvt_audit"):
    import api.server as server

    store = UsageStore(str(tmp_path / f"{name}.db"))
    store.create_key(hash_key(raw_key), name, owner_id=f"owner-{name}")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    return server, store, TestClient(server.app)


def _pin_fallback_registry(server, monkeypatch):
    """Make verdict/score assertions deterministic regardless of whether the
    brevitas.audit_capabilities registry module has shipped yet."""
    monkeypatch.setattr(server, "_audit_capability_registry",
                        lambda: [dict(c) for c in server._AUDIT_CAPABILITY_FALLBACK])


def _cap(body, needle):
    matches = [c for c in body["capabilities"] if needle in c["id"]]
    assert matches, f"no capability id containing {needle!r} in {body['capabilities']}"
    return matches[0]


def test_audit_requires_auth_and_registers_rate_limit(tmp_path, monkeypatch):
    server, _store, client = _setup(tmp_path, monkeypatch, "audit-auth")

    assert client.get("/v1/audit").status_code == 401
    assert client.get("/v1/audit",
                      headers={"X-Brevitas-Key": "bvt_wrong"}).status_code == 401
    # slowapi registers decorated routes by module-qualified name; the audit
    # endpoint must share the stats-family 120/minute budget.
    limits = server.limiter._route_limits["api.server.audit_report"]
    assert ["120 per 1 minute"] == [str(limit.limit) for limit in limits]


def test_empty_org_reports_unknown_verdicts_and_caveat(tmp_path, monkeypatch):
    server, _store, client = _setup(tmp_path, monkeypatch, "audit-empty")

    response = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"schema", "source", "scanned_at", "providers_detected",
                         "score", "verdict", "capabilities", "caveats", "traffic"}
    assert body["schema"] == "brevitas.audit.v1"
    assert body["source"] == "traffic"
    assert body["providers_detected"] == []
    # Zero rows measure nothing: no capability may claim anything — not even
    # not_applicable — and the overall verdict must not read "well_optimized".
    assert body["capabilities"], "registry produced no capabilities"
    assert {c["verdict"] for c in body["capabilities"]} == {"unknown"}
    assert all(c["evidence"] == [] for c in body["capabilities"])
    assert body["score"] == 0
    assert body["verdict"] == "not_measured"
    assert any("no proxied traffic" in caveat for caveat in body["caveats"])
    assert any("static" in caveat for caveat in body["caveats"])
    assert body["traffic"]["window_rows"] == 0
    assert body["traffic"]["native_cache_discount_usd"] == 0.0


def test_caller_owned_anthropic_markers_are_already_implemented(tmp_path, monkeypatch):
    server, store, client = _setup(tmp_path, monkeypatch, "audit-caller")
    _pin_fallback_registry(server, monkeypatch)

    for i in range(25):
        store.record_usage(
            hash_key("bvt_audit"), 500, 500, owner_id="owner-audit-caller",
            provider="anthropic", model="claude-sonnet-4-6",
            strategy="native_cache", cache_attributable=False,
            cached_input_tokens=400, fresh_input_tokens=100,
            native_cache_discount_usd=0.001, cache_write_tokens=50,
            cache_write_1h_tokens=50, session_id=f"session-{i % 5}",
            request_id=f"caller-{i}",
        )

    body = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"}).json()
    assert body["providers_detected"] == ["anthropic"]

    cache_control = _cap(body, "cache_control")
    # The honesty rule this feature exists for: a prospect whose own code
    # already sets cache_control must NEVER be shown an opportunity here.
    assert cache_control["verdict"] == "already_implemented"
    assert cache_control["estimated_impact"] is None
    assert any("caller-owned" in evidence for evidence in cache_control["evidence"])

    stability = _cap(body, "stabil")
    assert stability["verdict"] == "already_implemented"  # 80% hit rate
    assert _cap(body, "ttl")["verdict"] == "already_implemented"  # 1h writes
    # Static-only capabilities defer to the code scan rather than guessing.
    for needle in ("metering", "batching", "prompt_cache_key"):
        cap = _cap(body, needle)
        assert cap["verdict"] in ("unknown", "not_applicable")
    assert _cap(body, "exact")["verdict"] == "unknown"
    # Warming is the only measured opportunity (nobody self-warms), which
    # lands this org in the clean "you are already well-optimized" verdict.
    warming = _cap(body, "warming")
    assert warming["verdict"] == "opportunity"
    assert warming["estimated_impact"] == "high"
    assert any("approximation" in e for e in warming["evidence"])
    assert body["verdict"] == "well_optimized"
    assert 0 < body["score"] < 25
    # Real dollar context comes from priced rows, never fabricated.
    assert body["traffic"]["native_cache_discount_usd"] == 0.025
    assert body["traffic"]["attributable_discount_usd"] == 0.0
    assert body["traffic"]["cache_hit_rate_pct"] == 80.0


def test_write_only_anthropic_rows_are_not_an_opportunity(tmp_path, monkeypatch):
    """Org that just adopted cache_control: cache WRITES visible, zero reads.

    Anthropic only emits cache-write tokens for requests carrying cache_control
    markers, so the audit must never answer 'opportunity: no cache markers'
    one line away from write tokens proving the markers exist — the prospect
    refutes it with the same JSON response.
    """
    server, store, client = _setup(tmp_path, monkeypatch, "audit-writes")
    _pin_fallback_registry(server, monkeypatch)

    for i in range(25):
        store.record_usage(
            hash_key("bvt_audit"), 2500, 2500, owner_id="owner-audit-writes",
            provider="anthropic", model="claude-sonnet-4-6",
            strategy="passthrough", cache_attributable=False,
            fresh_input_tokens=2500, cache_write_tokens=2000,
            cache_write_5m_tokens=2000, session_id=f"session-{i % 5}",
            request_id=f"write-{i}",
        )

    body = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"}).json()
    cache_control = _cap(body, "cache_control")
    assert cache_control["verdict"] == "already_implemented"
    assert cache_control["estimated_impact"] is None
    assert any("wrote" in evidence for evidence in cache_control["evidence"])
    assert "cache_control markers" in cache_control["detail"]
    # Byte-stability must not blame "unstable prompt bytes" on the same data:
    # writes without reads also fits a cold, recently adopted cache.
    stability = _cap(body, "stabil")
    assert stability["verdict"] == "unknown"
    assert "cold" in stability["detail"]
    # TTL still reads 'partial' (caching active, default 5m) — consistent with
    # cache_control 'already_implemented', no self-contradiction in one report.
    assert _cap(body, "ttl")["verdict"] == "partial"


def test_write_only_brevitas_owned_rows_credit_brevitas(tmp_path, monkeypatch):
    server, store, client = _setup(tmp_path, monkeypatch, "audit-bwrites")
    _pin_fallback_registry(server, monkeypatch)

    for i in range(25):
        store.record_usage(
            hash_key("bvt_audit"), 2500, 2500, owner_id="owner-audit-bwrites",
            provider="anthropic", model="claude-sonnet-4-6",
            strategy="native_cache", cache_attributable=True,
            fresh_input_tokens=2500, cache_write_tokens=2000,
            cache_write_5m_tokens=2000, request_id=f"bwrite-{i}",
        )

    body = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"}).json()
    cache_control = _cap(body, "cache_control")
    assert cache_control["verdict"] == "already_implemented"
    assert "Brevitas-managed" in cache_control["detail"]


def test_short_prompt_workloads_are_not_accused_of_byte_churn(tmp_path, monkeypatch):
    """Prompts under the ~1024-token provider cache floor can NEVER show
    cached tokens, however byte-stable they are; blaming 'unstable prompt
    bytes (timestamps, uuids, unsorted json)' would be structurally wrong."""
    server, store, client = _setup(tmp_path, monkeypatch, "audit-short")
    _pin_fallback_registry(server, monkeypatch)

    for i in range(60):
        store.record_usage(
            hash_key("bvt_audit"), 500, 500, owner_id="owner-audit-short",
            provider="openai", model="gpt-4o-mini", strategy="passthrough",
            fresh_input_tokens=500, session_id=f"session-{i % 6}",
            request_id=f"short-{i}",
        )

    body = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"}).json()
    stability = _cap(body, "stabil")
    assert stability["verdict"] == "not_applicable"
    assert stability["estimated_impact"] is None
    assert any("cache floor" in evidence for evidence in stability["evidence"])
    assert any("approximation" in evidence for evidence in stability["evidence"])


def test_mixed_prompt_sizes_gate_small_providers_out_of_stability(tmp_path, monkeypatch):
    """Cache-floor gating is per provider: a large-prompt provider with a
    healthy hit rate must not be dragged down by a sub-floor provider."""
    server, store, client = _setup(tmp_path, monkeypatch, "audit-mixed")
    _pin_fallback_registry(server, monkeypatch)

    for i in range(30):
        store.record_usage(
            hash_key("bvt_audit"), 4000, 4000, owner_id="owner-audit-mixed",
            provider="anthropic", model="claude-sonnet-4-6",
            strategy="native_cache", cache_attributable=False,
            cached_input_tokens=3000, fresh_input_tokens=1000,
            request_id=f"big-{i}",
        )
        store.record_usage(
            hash_key("bvt_audit"), 400, 400, owner_id="owner-audit-mixed",
            provider="openai", model="gpt-4o-mini", strategy="passthrough",
            fresh_input_tokens=400, request_id=f"small-{i}",
        )

    body = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"}).json()
    stability = _cap(body, "stabil")
    assert stability["verdict"] == "already_implemented"  # 75% on anthropic
    assert any("excluded" in evidence and "openai" in evidence
               for evidence in stability["evidence"])


def test_absence_of_evidence_never_becomes_opportunity(tmp_path, monkeypatch):
    server, store, client = _setup(tmp_path, monkeypatch, "audit-honesty")
    _pin_fallback_registry(server, monkeypatch)

    # 25 xai rows with zero cache reads: headers are not stored, so conv-id
    # affinity stays unknown (pointing at the static scanner), not opportunity.
    for i in range(25):
        store.record_usage(
            hash_key("bvt_audit"), 100, 100, owner_id="owner-audit-honesty",
            provider="xai", model="grok-4", strategy="passthrough",
            fresh_input_tokens=100, request_id=f"xai-{i}",
        )

    body = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"}).json()
    xai = _cap(body, "xai")
    assert xai["verdict"] == "unknown"
    assert "static" in xai["detail"]
    # 100-token prompts sit under the provider cache floor, so zero hits is
    # structural and byte stability cannot change the bill: not_applicable.
    assert _cap(body, "stabil")["verdict"] == "not_applicable"
    # Anthropic capabilities are not_applicable — no anthropic traffic exists,
    # which is a different (and defensible) claim than opportunity.
    assert _cap(body, "cache_control")["verdict"] == "not_applicable"
    assert _cap(body, "ttl")["verdict"] == "not_applicable"


def test_stream_receipts_exact_replays_and_warming_verdicts(tmp_path, monkeypatch):
    server, store, client = _setup(tmp_path, monkeypatch, "audit-verdicts")
    _pin_fallback_registry(server, monkeypatch)

    for i in range(10):
        store.record_usage(
            hash_key("bvt_audit"), 100, 100, owner_id="owner-audit-verdicts",
            provider="openai", model="gpt-4o-mini", is_stream=True,
            strategy="passthrough:missing_receipt", request_id=f"stream-{i}",
        )
    store.record_usage(
        hash_key("bvt_audit"), 100, 0, owner_id="owner-audit-verdicts",
        provider="openai", model="gpt-4o-mini", strategy="exact_cache",
        calls_avoided=1, request_id="replay-1",
    )

    body = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"}).json()
    stream = _cap(body, "stream")
    assert stream["verdict"] == "opportunity"  # every streamed row lacked usage
    assert any("0 of 10" in evidence for evidence in stream["evidence"])
    exact = _cap(body, "exact")
    assert exact["verdict"] == "already_implemented"
    assert any("1 provider calls avoided" in e for e in exact["evidence"])
    # Warming is anthropic-scoped: an openai-only org must see not_applicable
    # rather than a pitch for a capability its providers cannot use.
    assert _cap(body, "warming")["verdict"] == "not_applicable"
    assert body["traffic"]["calls_avoided"] == 1


def test_sparse_traffic_never_yields_warming_pitch_or_congratulation(tmp_path, monkeypatch):
    server, store, client = _setup(tmp_path, monkeypatch, "audit-sparse")
    _pin_fallback_registry(server, monkeypatch)

    # 10 anthropic rows: below _AUDIT_MIN_EVIDENCE_ROWS, nothing optimized.
    for i in range(10):
        store.record_usage(
            hash_key("bvt_audit"), 100, 100, owner_id="owner-audit-sparse",
            provider="anthropic", model="claude-sonnet-4-6",
            fresh_input_tokens=100, request_id=f"sparse-{i}",
        )

    body = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"}).json()
    # A handful of requests cannot establish warming ROI: no pitch.
    assert _cap(body, "warming")["verdict"] == "unknown"
    # And a low score earned from sparse/unknown data must not read as a
    # certification — the self-contradicting "well_optimized above an
    # Opportunity row" case.
    assert body["verdict"] != "well_optimized"


def test_audit_is_scoped_to_the_calling_key(tmp_path, monkeypatch):
    server, store, client = _setup(tmp_path, monkeypatch, "audit-scope")
    store.create_key(hash_key("bvt_other"), "other-tenant", owner_id="owner-other")
    store.record_usage(
        hash_key("bvt_other"), 100, 100, owner_id="owner-other",
        provider="xai", model="grok-4", cached_input_tokens=50,
        fresh_input_tokens=50, request_id="other-1",
    )
    store.record_usage(
        hash_key("bvt_audit"), 100, 100, owner_id="owner-audit-scope",
        provider="anthropic", model="claude-sonnet-4-6",
        fresh_input_tokens=100, request_id="mine-1",
    )

    body = client.get("/v1/audit", headers={"X-Brevitas-Key": "bvt_audit"}).json()
    assert body["providers_detected"] == ["anthropic"]
    assert body["traffic"]["window_rows"] == 1


def test_store_audit_metrics_counts_ownership_streams_and_ttl(tmp_path):
    store = UsageStore(str(tmp_path / "metrics.db"))
    kh = hash_key("bvt_metrics")
    store.create_key(kh, "metrics", owner_id="owner-metrics")
    rows = [
        # caller-owned anthropic cache read in a repeat session
        dict(provider="anthropic", strategy="native_cache",
             cached_input_tokens=300, fresh_input_tokens=100,
             cache_attributable=False, session_id="s1"),
        dict(provider="anthropic", strategy="native_cache",
             cached_input_tokens=100, fresh_input_tokens=100,
             cache_attributable=False, session_id="s1"),
        # Brevitas-owned read with a 1h cache write
        dict(provider="anthropic", strategy="native_cache",
             cached_input_tokens=200, fresh_input_tokens=0,
             cache_attributable=True, cache_write_tokens=80,
             cache_write_1h_tokens=80),
        # streamed openai rows: one with a receipt, one without
        dict(provider="openai", strategy="passthrough", is_stream=True,
             fresh_input_tokens=100),
        dict(provider="openai", strategy="passthrough:missing_receipt",
             is_stream=True, fresh_input_tokens=100),
        # exact replay
        dict(provider="openai", strategy="exact_cache", calls_avoided=2),
    ]
    for i, row in enumerate(rows):
        store.record_usage(kh, 100, 100, owner_id="owner-metrics",
                           model="m", request_id=f"r-{i}", **row)

    metrics = store.audit_metrics(kh)
    assert metrics["schema"] == "brevitas.audit-metrics.v1"
    assert metrics["window_rows"] == 6
    assert metrics["providers"] == ["anthropic", "openai"]
    cache = metrics["cache"]
    assert cache["caller_owned_cache_rows"] == 2
    assert cache["brevitas_owned_cache_rows"] == 1
    assert cache["cached_input_tokens"] == 600
    assert cache["fresh_input_tokens"] == 400
    assert cache["hit_rate_pct"] == 60.0
    anthropic = metrics["per_provider"]["anthropic"]
    assert anthropic["caller_owned_cache_rows"] == 2
    assert anthropic["brevitas_owned_cache_rows"] == 1
    assert anthropic["hit_rate_pct"] == 75.0
    # Per-provider write evidence: writes prove cache markers before any read
    # lands, with the same cache_attributable ownership inference as reads.
    assert anthropic["cache_write_rows"] == 1
    assert anthropic["cache_write_tokens"] == 80
    assert anthropic["brevitas_owned_write_rows"] == 1
    assert anthropic["caller_owned_write_rows"] == 0
    streaming = metrics["streaming"]
    assert streaming["streamed_rows"] == 2
    assert streaming["streamed_missing_receipt_rows"] == 1
    assert streaming["usage_visibility_pct"] == 50.0
    repeat = metrics["repeat"]
    assert repeat["exact_cache_rows"] == 1
    assert repeat["calls_avoided"] == 2
    assert repeat["repeat_sessions"] == 1
    assert repeat["repeat_session_rows"] == 2
    ttl = metrics["ttl"]
    assert ttl["cache_write_1h_tokens"] == 80
    assert ttl["brevitas_owned_1h_rows"] == 1
    assert ttl["caller_owned_1h_rows"] == 0
    # warming never configured for this org: sentinel nulls, not zeros
    warming = metrics["warming"]
    assert warming["configured"] is False
    assert warming["warm_pings"] is None
    # window approximations are declared, never silent
    assert any("bounded window" in a for a in metrics["approximations"])
    assert any("cache_attributable" in a for a in metrics["approximations"])


def test_audit_metrics_helper_tolerates_hostile_rows():
    metrics = _audit_metrics([
        {"provider": "", "model": "", "strategy": None,
         "cached_input_tokens": "n/a" and None, "is_stream": None},
        {"model": "claude-sonnet-4-6", "cached_input_tokens": 10,
         "strategy": "native_cache"},
    ], None)
    # blank provider falls back to model inference, then "unknown"
    assert metrics["providers"] == ["anthropic", "unknown"]
    assert metrics["cache"]["caller_owned_cache_rows"] == 1


def _daily_rows(count, session_id, spacing_s):
    """`count` cache-capable anthropic rows sharing one session_id, spaced
    `spacing_s` apart, with zero cache reads and no cache writes."""
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return [dict(provider="anthropic", model="claude-3-5-sonnet",
                 strategy="passthrough", cached_input_tokens=0,
                 fresh_input_tokens=4000, session_id=session_id,
                 ts=(base + timedelta(seconds=i * spacing_s)).isoformat())
            for i in range(count)]


def test_repeat_sessions_are_time_bounded_to_the_provider_cache_ttl():
    """session_id is a stable per-model bucket hash (brevitas/proxy.py
    `_stable_session_id`), so it survives proxy restarts and TTL eviction and
    can collide across weeks. Rows that far apart are NOT evidence of repeat
    traffic: a cache entry could not have been live, so a 0% hit rate there is
    structural, not prompt-byte churn. Guards the customer-facing verdict."""
    import api.server as server

    spread = _audit_metrics(_daily_rows(25, "sess_stable", 86400), None)
    assert spread["repeat"]["repeat_session_rows"] == 0
    assert spread["repeat"]["repeat_sessions"] == 0
    verdict, _evidence, rationale = server._audit_verdict_byte_stability(spread)
    assert verdict == "unknown"
    assert "may simply not repeat" in rationale

    # Control: the same 25 rows minutes apart are genuine repeat traffic — a
    # live cache entry was possible — so the opportunity verdict still fires.
    tight = _audit_metrics(_daily_rows(25, "sess_stable", 300), None)
    assert tight["repeat"]["repeat_session_rows"] == 25
    assert tight["repeat"]["repeat_sessions"] == 1
    verdict, evidence, _rationale = server._audit_verdict_byte_stability(tight)
    assert verdict == "opportunity"
    assert any("inside an hour" in item for item in evidence)

    # A restart in the middle splits one bucket id into two clusters: only the
    # rows that actually clustered count.
    mixed = _audit_metrics(
        _daily_rows(3, "sess_stable", 300)
        + _daily_rows(1, "sess_stable", 300)[:1]
        + [dict(provider="anthropic", model="claude-3-5-sonnet",
                strategy="passthrough", cached_input_tokens=0,
                fresh_input_tokens=4000, session_id="sess_stable",
                ts=datetime(2026, 7, 9, tzinfo=timezone.utc).isoformat())],
        None)
    assert mixed["repeat"]["repeat_session_rows"] == 4
    # the lone week-later row is excluded, and the duplicate ts collapses in
    assert mixed["repeat"]["repeat_sessions"] == 1
    assert any("within one hour" in a for a in mixed["approximations"])
