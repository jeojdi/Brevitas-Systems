"""Cross-run state persistence (state_store) — decision state survives a restart,
content never touches disk, corrupt files fail safe. Deterministic; no network."""
from __future__ import annotations

import json
import uuid

from token_efficiency_model.lossless import engine, state_store
from token_efficiency_model.lossless.router import BrevitasRouter
from token_efficiency_model.lossless.shared_prefix import _default as shared

CTX = ["stable system prompt " * 120, "shared reference corpus " * 200]


def _uid(p):
    return f"{p}-{uuid.uuid4().hex[:8]}"


def test_router_state_survives_restart(tmp_path):
    path = str(tmp_path / "state.json")
    routers = {"k1": BrevitasRouter(provider="deepseek")}
    sid = _uid("s")
    routers["k1"].decide(sid, CTX, "q1")          # fingerprint stored
    routers["k1"].observe_usage(sid, 5000, 4000)  # learned cache behavior
    assert state_store.save(path, routers)

    # "restart": brand-new registry, restore from disk
    fresh: dict = {}
    n = state_store.load(path, fresh, lambda prov: BrevitasRouter(provider=prov))
    assert n >= 1 and "k1" in fresh
    d = fresh["k1"].decide(sid, CTX, "q2")        # identical context after restart
    assert d.repeat_rate == 1.0, "restored fingerprint must recognize the repeat"
    # a cold router (no restore) would have seen repeat_rate 0.0
    cold = BrevitasRouter(provider="deepseek")
    assert cold.decide(sid, CTX, "q2").repeat_rate == 0.0


def test_b9_lock_and_promotion_survive_restart(tmp_path):
    path = str(tmp_path / "state.json")
    pipe = _uid("pipe")
    engine._b9_pipes[pipe] = {"reordered": True, "locked": True,
                              "pre_hit": 0.6, "post_hit": 0.1, "post_n": 3}
    ps = shared._pipelines
    from token_efficiency_model.lossless.shared_prefix import _PipelineState
    st = _PipelineState()
    st.seen = {"h1": {"a", "b"}}
    st.promo_order = {"h1": 0}
    ps[pipe] = st

    assert state_store.save(path, {})
    engine._b9_pipes.pop(pipe)
    ps.pop(pipe)

    state_store.load(path, {}, lambda p: BrevitasRouter(provider=p))
    assert engine._b9_pipes[pipe]["locked"] is True, "do-no-harm lock must persist"
    assert ps[pipe].promo_order == {"h1": 0}, "frozen promotion order must persist"
    assert ps[pipe].seen == {"h1": {"a", "b"}}


def test_snapshot_is_content_free(tmp_path):
    path = str(tmp_path / "state.json")
    secret = "SECRET-CUSTOMER-DATA-" + uuid.uuid4().hex
    routers = {"k": BrevitasRouter(provider="openai")}
    routers["k"].decide(_uid("s"), [secret * 50], "what is in the data?")
    state_store.save(path, routers)
    raw = open(path).read()
    assert secret not in raw, "message content must NEVER be persisted"
    assert "what is in the data" not in raw


def test_corrupt_or_missing_file_fails_safe(tmp_path):
    assert state_store.load(str(tmp_path / "nope.json"), {}, None) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert state_store.load(str(bad), {}, None) == 0
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"v": 1, "ts": 0, "routers": {}}))  # week-old snapshot
    assert state_store.load(str(old), {}, None) == 0
