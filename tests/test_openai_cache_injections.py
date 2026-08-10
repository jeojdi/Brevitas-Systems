"""OpenAI prompt-cache injections: routing key + explicit breakpoints.

Both are approved lossless injections (RL_PREDICTIVE_WARMING_PLAN.md Decision 2) and
both ship DEFAULT OFF behind their own kill switch. The properties pinned here are the
ones that make them safe to turn on:

  * nothing is injected unless an operator sets the flag;
  * the injected key is opaque, scoped to (org credential, end customer), and carries
    no prompt content;
  * a caller who set ANY cache directive of their own gets nothing from us;
  * prompt_cache_options is never written without a breakpoint in the same call;
  * cache_attributable is true only where Brevitas itself placed the breakpoint --
    a routing key improves matching but cannot prove a discount was ours.
"""
from __future__ import annotations

import json
import re
import time

import httpx

from brevitas.warming import extract_warm_prefix
from token_efficiency_model.lossless import provider_cache
from token_efficiency_model.lossless.engine import optimize_request
from token_efficiency_model.lossless.provider_cache import (
    anthropic_min_tokens,
    apply_openai_cache,
)
from token_efficiency_model.lossless.router import BrevitasRouter
from tests.test_proxy_message_structure import _mock_proxy

_KEY_RE = re.compile(r"^bx1:[0-9a-f]{16}(:\d+)?$")
_TENANT_A = "a1b2c3d4e5f60718" + "9" * 48          # sha256-shaped, 64 hex
_TENANT_B = "f0e1d2c3b4a59687" + "1" * 48


def _long(n_words: int = 2000) -> str:
    return " ".join(["lorem"] * n_words)


def _body(stable: str | None = None, model: str = "gpt-5.6") -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": stable if stable is not None else _long()},
            {"role": "user", "content": "the volatile question"},
        ],
    }


def _reset_shard_state():
    provider_cache._KEY_RPM.clear()
    provider_cache._KEY_SHARDS.clear()


def _openai_meta(body: dict, monkeypatch, tenant_key: str = _TENANT_A) -> dict:
    """Run the real engine entry point on the openai branch."""
    monkeypatch.setattr(provider_cache, "_KEY_RPM", {})
    monkeypatch.setattr(provider_cache, "_KEY_SHARDS", {})
    return optimize_request(body, "openai", BrevitasRouter(), "session-1",
                            tenant_key=tenant_key)


_OK = {
    "id": "chatcmpl-1", "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 2100, "completion_tokens": 1,
              "prompt_tokens_details": {"cached_tokens": 0}},
}


def _forward(monkeypatch, body: dict) -> dict:
    """Send one request through the proxy and return the body actually forwarded."""
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json=_OK)

    _, client = _mock_proxy(monkeypatch, handler)
    monkeypatch.setenv("BREVITAS_PROVIDER", "openai")
    response = client.post("/v1/chat/completions", json=body,
                           headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    return forwarded[0]


# --- prompt_cache_key ------------------------------------------------------- #
def test_openai_cache_key_off_by_default(monkeypatch):
    monkeypatch.delenv("BREVITAS_OPENAI_CACHE_KEY", raising=False)
    body = _body()
    meta = _openai_meta(body, monkeypatch)
    assert "prompt_cache_key" not in body
    assert meta["openai_cache_key_added"] is False
    # ...and the same through the whole proxy, on the bytes actually forwarded.
    assert "prompt_cache_key" not in _forward(monkeypatch, _body())


def test_openai_cache_key_injected_when_enabled(monkeypatch):
    monkeypatch.setenv("BREVITAS_OPENAI_CACHE_KEY", "1")
    stable = _long()
    body = _body(stable)
    meta = _openai_meta(body, monkeypatch)
    assert meta["openai_cache_key_added"] is True
    assert _KEY_RE.match(body["prompt_cache_key"]), body["prompt_cache_key"]
    # Lossless: the injection adds one metadata field and touches no prompt byte.
    assert body["messages"] == _body(stable)["messages"]
    forwarded = _forward(monkeypatch, _body(stable))
    assert _KEY_RE.match(forwarded["prompt_cache_key"])
    assert forwarded["messages"] == _body(stable)["messages"]


def test_openai_cache_key_opaque_and_tenant_scoped():
    _reset_shard_state()
    stable = _long()
    a, b = _body(stable), _body(stable)
    apply_openai_cache(a, tenant_key=_TENANT_A, inject_key=True)
    apply_openai_cache(b, tenant_key=_TENANT_B, inject_key=True)
    assert a["prompt_cache_key"] != b["prompt_cache_key"]
    # Opaque: neither the credential digest beyond 16 chars nor any prompt text leaks.
    assert _TENANT_A not in a["prompt_cache_key"]
    assert "lorem" not in a["prompt_cache_key"]

    # One tenant, two different prefix families -> same base (the key is scoped to the
    # customer, not to the prompt; sharding is the only thing that may suffix it).
    _reset_shard_state()
    one, two = _body(_long(1500)), _body(_long(2500))
    apply_openai_cache(one, tenant_key=_TENANT_A, inject_key=True)
    apply_openai_cache(two, tenant_key=_TENANT_A, inject_key=True)
    assert one["prompt_cache_key"].split(":")[:2] == two["prompt_cache_key"].split(":")[:2]


def test_caller_supplied_prompt_cache_key_wins():
    _reset_shard_state()
    body = _body()
    body["prompt_cache_key"] = "customer-owned-key"
    plan = apply_openai_cache(body, tenant_key=_TENANT_A, inject_key=True,
                              explicit_breakpoint=True)
    assert body["prompt_cache_key"] == "customer-owned-key"
    assert plan.owner == "caller"
    assert plan.key_added is False and plan.breakpoint_added is False
    assert "prompt_cache_options" not in body


def test_openai_cache_key_shard_stable_per_prefix_family(monkeypatch):
    _reset_shard_state()
    base = "bx1:" + _TENANT_A[:16]
    # Force the rate estimator high enough to demand the maximum shard count.
    monkeypatch.setitem(provider_cache._KEY_RPM, base, (5000.0, time.time()))
    stable = _long()

    first = _body(stable)
    apply_openai_cache(first, tenant_key=_TENANT_A, inject_key=True)
    key = first["prompt_cache_key"]
    assert key.startswith(base + ":")
    shard = int(key.rsplit(":", 1)[1])
    assert 0 <= shard < 16
    assert provider_cache._KEY_SHARDS[base][0] == 16

    # Same stable prefix family -> same shard every time. A family that hopped shards
    # would abandon the machine already holding its prefix, which is the whole point.
    for _ in range(5):
        again = _body(stable)
        apply_openai_cache(again, tenant_key=_TENANT_A, inject_key=True)
        assert again["prompt_cache_key"] == key

    # A quiet moment does NOT re-merge a family onto a busy machine mid-flight:
    # the step-down is rate-limited and deadbanded, so the very next request
    # after the rate collapses is still on the shard it was on.
    provider_cache._KEY_RPM[base] = (0.0, time.time())
    idle = _body(stable)
    apply_openai_cache(idle, tenant_key=_TENANT_A, inject_key=True)
    assert provider_cache._KEY_SHARDS[base][0] == 16
    assert idle["prompt_cache_key"] == key


def test_openai_cache_key_shard_count_decays_when_the_burst_ends(monkeypatch):
    """A burst must not pin a base at 16 shards for the life of the process.

    Monotone-non-decreasing sounds conservative and is the opposite: it leaves a
    long-idle family split 16 ways, fragmenting exactly the residency the key
    buys -- and, because the key is a chain extra, splitting the prefix tree into
    16 roots so dedup groups stop forming.
    """
    _reset_shard_state()
    base = "bx1:" + _TENANT_A[:16]
    stable = _long()
    clock = [time.time()]
    monkeypatch.setattr(provider_cache.time, "time", lambda: clock[0])

    # A 90-second burst pins the maximum.
    provider_cache._KEY_RPM[base] = (5000.0, clock[0])
    apply_openai_cache(_body(stable), tenant_key=_TENANT_A, inject_key=True)
    assert provider_cache._KEY_SHARDS[base][0] == 16

    # The burst ends. One step down per decay interval, never faster, and only
    # once the rate is a clear margin under what the lower count supports.
    provider_cache._KEY_RPM[base] = (0.2, clock[0])
    clock[0] += provider_cache._KEY_SHARD_DECAY_SECONDS - 1
    apply_openai_cache(_body(stable), tenant_key=_TENANT_A, inject_key=True)
    assert provider_cache._KEY_SHARDS[base][0] == 16, "decay must be rate limited"

    for expected in (15, 14, 13):
        provider_cache._KEY_RPM[base] = (0.2, clock[0])
        clock[0] += provider_cache._KEY_SHARD_DECAY_SECONDS
        apply_openai_cache(_body(stable), tenant_key=_TENANT_A, inject_key=True)
        assert provider_cache._KEY_SHARDS[base][0] == expected

    # And it walks all the way back to a single unsharded key.
    for _ in range(40):
        provider_cache._KEY_RPM[base] = (0.2, clock[0])
        clock[0] += provider_cache._KEY_SHARD_DECAY_SECONDS
        body = _body(stable)
        apply_openai_cache(body, tenant_key=_TENANT_A, inject_key=True)
    assert provider_cache._KEY_SHARDS[base][0] == 1
    assert body["prompt_cache_key"] == base


def test_openai_cache_key_shard_count_rises_immediately(monkeypatch):
    """Queueing is the urgent failure, so the count never waits to go UP."""
    _reset_shard_state()
    base = "bx1:" + _TENANT_A[:16]
    stable = _long()
    apply_openai_cache(_body(stable), tenant_key=_TENANT_A, inject_key=True)
    assert provider_cache._KEY_SHARDS[base][0] == 1
    provider_cache._KEY_RPM[base] = (5000.0, time.time())
    apply_openai_cache(_body(stable), tenant_key=_TENANT_A, inject_key=True)
    assert provider_cache._KEY_SHARDS[base][0] == 16


def test_openai_cache_key_unsharded_below_rate_threshold():
    _reset_shard_state()
    body = _body()
    apply_openai_cache(body, tenant_key=_TENANT_A, inject_key=True)
    assert body["prompt_cache_key"] == "bx1:" + _TENANT_A[:16]


# --- explicit breakpoints --------------------------------------------------- #
def test_openai_breakpoints_off_by_default(monkeypatch):
    monkeypatch.delenv("BREVITAS_OPENAI_BREAKPOINTS", raising=False)
    monkeypatch.delenv("BREVITAS_OPENAI_CACHE_BREAKPOINTS", raising=False)
    body = _body()
    meta = _openai_meta(body, monkeypatch)
    assert meta["openai_cache_breakpoint_added"] is False
    assert "prompt_cache_options" not in body


def test_openai_breakpoints_legacy_alias_honored(monkeypatch):
    monkeypatch.delenv("BREVITAS_OPENAI_BREAKPOINTS", raising=False)
    monkeypatch.setenv("BREVITAS_OPENAI_CACHE_BREAKPOINTS", "1")
    body = _body()
    assert _openai_meta(body, monkeypatch)["openai_cache_breakpoint_added"] is True

    # The current name wins whenever it is set, including to a falsy value.
    monkeypatch.setenv("BREVITAS_OPENAI_BREAKPOINTS", "0")
    body = _body()
    assert _openai_meta(body, monkeypatch)["openai_cache_breakpoint_added"] is False

    monkeypatch.setenv("BREVITAS_OPENAI_BREAKPOINTS", "1")
    monkeypatch.delenv("BREVITAS_OPENAI_CACHE_BREAKPOINTS", raising=False)
    body = _body()
    assert _openai_meta(body, monkeypatch)["openai_cache_breakpoint_added"] is True


def test_openai_breakpoint_placed_at_stable_prefix_boundary():
    _reset_shard_state()
    body = _body()
    plan = apply_openai_cache(body, tenant_key=_TENANT_A, explicit_breakpoint=True)
    assert plan.breakpoint_added and plan.owner == "brevitas"
    assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    # The marker sits on the LAST stable block (the system turn), promoted from a
    # string to a one-element block list without altering the text...
    stable_block = body["messages"][0]["content"][0]
    assert stable_block["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert stable_block["text"] == _long()
    # ...and the volatile final turn is untouched.
    assert body["messages"][-1] == {"role": "user", "content": "the volatile question"}


def test_openai_breakpoint_stands_down_on_any_caller_cache_directive():
    directives = [
        ("key", lambda b: b.__setitem__("prompt_cache_key", "caller-key")),
        ("options", lambda b: b.__setitem__(
            "prompt_cache_options", {"mode": "explicit", "ttl": "1h"})),
        ("embedded", lambda b: b["messages"].__setitem__(0, {
            "role": "system",
            "content": [{"type": "text", "text": _long(),
                         "prompt_cache_breakpoint": {"mode": "explicit"}}]})),
    ]
    for label, mutate in directives:
        _reset_shard_state()
        body = _body()
        mutate(body)
        before = json.loads(json.dumps(body))
        plan = apply_openai_cache(body, tenant_key=_TENANT_A, inject_key=True,
                                  explicit_breakpoint=True)
        assert plan.owner == "caller", label
        assert plan.breakpoint_added is False, label
        # The caller's own cache policy survives byte-identical: their key, their
        # options, their breakpoint, and every message exactly as they wrote it.
        assert body["messages"] == before["messages"], label
        assert body.get("prompt_cache_options") == before.get(
            "prompt_cache_options"), label
        # The routing key is orthogonal to boundary ownership: it is added only when
        # the caller did not name one, and it never turns their request into ours.
        assert plan.key_added is (label != "key"), label
        if label == "key":
            assert body["prompt_cache_key"] == "caller-key"


def test_openai_options_never_without_breakpoint():
    """prompt_cache_options and the breakpoint are written by one code path, together.

    Explicit mode without a breakpoint tells OpenAI to cache only at boundaries the
    request does not contain -- i.e. it disables caching while still being billed for
    the mode. The coupling is structural; this pins it across every reachable shape.
    """
    _reset_shard_state()
    shapes = [
        _body(),                                                    # str content
        {"model": "gpt-5.6", "messages": [                          # block-list content
            {"role": "system", "content": [{"type": "text", "text": _long()}]},
            {"role": "user", "content": "q"}]},
        {"model": "gpt-5.6", "messages": [                          # stable block in last turn
            {"role": "user", "content": [{"type": "text", "text": _long()},
                                         {"type": "text", "text": "q"}]}]},
        {"model": "gpt-5.6", "messages": [                          # too short to cache
            {"role": "system", "content": "brief"},
            {"role": "user", "content": "q"}]},
        {"model": "gpt-5.6", "messages": [                          # single volatile turn
            {"role": "user", "content": _long()}]},
        {"model": "gpt-4o", "messages": [                           # unsupported model
            {"role": "system", "content": _long()},
            {"role": "user", "content": "q"}]},
    ]
    for shape in shapes:
        plan = apply_openai_cache(shape, tenant_key=_TENANT_A, inject_key=True,
                                  explicit_breakpoint=True)
        assert ("prompt_cache_options" in shape) == plan.breakpoint_added, shape["model"]


def test_openai_responses_operation_uses_input_text_block():
    _reset_shard_state()
    body = _body()
    body["_brevitas_operation"] = "responses"
    apply_openai_cache(body, tenant_key=_TENANT_A, explicit_breakpoint=True)
    assert body["messages"][0]["content"][0]["type"] == "input_text"


# --- attribution ------------------------------------------------------------ #
def test_cache_attributable_only_when_brevitas_placed_breakpoints(monkeypatch):
    # key only -> NOT attributable: OpenAI may have cached the same prefix anyway.
    monkeypatch.setenv("BREVITAS_OPENAI_CACHE_KEY", "1")
    monkeypatch.delenv("BREVITAS_OPENAI_BREAKPOINTS", raising=False)
    monkeypatch.delenv("BREVITAS_OPENAI_CACHE_BREAKPOINTS", raising=False)
    meta = _openai_meta(_body(), monkeypatch)
    assert meta["openai_cache_key_added"] is True
    assert meta["cache_attributable"] is False

    # breakpoint -> attributable: explicit mode is a boundary only we asked for.
    monkeypatch.setenv("BREVITAS_OPENAI_BREAKPOINTS", "1")
    meta = _openai_meta(_body(), monkeypatch)
    assert meta["openai_cache_breakpoint_added"] is True
    assert meta["cache_attributable"] is True

    # caller-owned -> not attributable, and nothing of ours was added.
    caller = _body()
    caller["prompt_cache_options"] = {"mode": "explicit", "ttl": "1h"}
    meta = _openai_meta(caller, monkeypatch)
    assert meta["cache_attributable"] is False
    assert meta["cache_control_owner"] == "caller"


# --- ordering: the injected key must reach the warm payload ----------------- #
def test_injected_key_rides_into_warm_payload(monkeypatch):
    """Injection runs before extract_warm_prefix, so pings address the same entry.

    If extraction ran first, the warm payload would carry no prompt_cache_key while
    live traffic carried one -- every ping would warm a cache entry nothing reads.
    """
    monkeypatch.setenv("BREVITAS_OPENAI_CACHE_KEY", "1")
    body = _body(_long() + " " + _long())
    _openai_meta(body, monkeypatch)
    injected = body["prompt_cache_key"]
    assert _KEY_RE.match(injected)

    prefix = extract_warm_prefix(body, "openai", "gpt-5.6", {}, {},
                                 upstream="https://api.openai.com/v1/chat/completions")
    assert prefix is not None
    assert prefix.payload["prompt_cache_key"] == injected


# --- included provider-table fix -------------------------------------------- #
def test_anthropic_min_tokens_opus5_is_512():
    assert anthropic_min_tokens("claude-opus-5-20260115") == 512
    # neighbouring rows are untouched
    assert anthropic_min_tokens("claude-opus-4-5-20250929") == 4096
    assert anthropic_min_tokens("claude-sonnet-4-5-20250929") == 1024
