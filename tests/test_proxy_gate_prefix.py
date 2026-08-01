"""The proxy gate, attacked.

_protect_model_proxy is the ONLY authentication a hosted proxy route has: every
handler in brevitas/proxy.py takes a bare `request: Request` with no Depends, and
the routes are plain routes on the main app. A path that does not match the gate
falls straight through to the handler, which still forwards the caller's own
provider credential upstream -- an unauthenticated, unmetered open relay over
Brevitas's egress. A path that routes but misses the gate also bills nothing.

So these tests are mostly bypass attempts. The shape of the contract:

  * every path the router can send to a proxy handler is PROTECTED or REFUSED;
  * REFUSED never reaches call_next;
  * the 13 literals behave exactly as they did before prefixes existed;
  * nothing outside the proxy namespace changes.
"""
import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.routing import Route

import api.server as server


PROTECTED = server._PROXY_GATE_PROTECTED
REFUSED = server._PROXY_GATE_REFUSED
OPEN = server._PROXY_GATE_OPEN

BEDROCK_MODEL = "us.anthropic.claude-opus-5-v1:0"
BEDROCK_PATH = f"/bedrock/model/{BEDROCK_MODEL}/invoke"

# A deployment named after its model is the commonest Azure convention and the
# reason the deployment charset has to allow ".". If the gate ever refuses this
# shape, every drop-in Azure customer breaks at once.
AZURE_PATH = "/azure/contoso-openai/openai/deployments/gpt-4.1/chat/completions"


def disposition(path, raw_path=None):
    """Classify as the middleware does, defaulting raw_path the way a server would."""
    if raw_path is None:
        raw_path = path.encode("utf-8")
    return server._proxy_gate_disposition(path, raw_path)


# --------------------------------------------------------------------------
# 1. The gate set is still coextensive with the router.
# --------------------------------------------------------------------------

def _leaf_routes(routes=None):
    """Every route the app can actually match, flattened.

    FastAPI 0.139 does NOT flatten `include_router` into `app.routes`: the
    included routes hang off a `_IncludedRouter` entry, so `app.routes` alone
    reports ZERO proxy routes. A parity check that walks only `app.routes` would
    therefore pass vacuously -- exactly the failure mode this file exists to
    prevent -- so the walk recurses.
    """
    for route in (server.app.routes if routes is None else routes):
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _leaf_routes(original.routes)
            continue
        nested = getattr(route, "routes", None)
        if nested:
            yield from _leaf_routes(nested)
            continue
        yield route


def _proxy_handler_routes():
    return [
        route for route in _leaf_routes()
        if getattr(getattr(route, "endpoint", None), "__module__", "") == "brevitas.proxy"
        and getattr(route, "path", "") != "/health"
    ]


def _literal_routes():
    return [route for route in _proxy_handler_routes() if "{" not in route.path]


def _parameterised_routes():
    return [route for route in _proxy_handler_routes() if "{" in route.path]


def test_the_route_walk_actually_finds_the_proxy_routes():
    """Guards the guard: a vacuous walk would make every parity test below pass."""
    assert len(_proxy_handler_routes()) == 20
    # The split is the whole point of the gate's two arms: 13 routes an exact
    # match can name, and 7 that carry a variable segment and can only be gated
    # by prefix -- Bedrock invoke/stream plus its two Converse refusals, and
    # Azure chat completions plus its two Responses refusals.
    assert len(_literal_routes()) == 13
    assert len(_parameterised_routes()) == 7


def test_every_registered_proxy_route_is_gated():
    """The structural property the audit confirmed, asserted continuously.

    Any route served by a brevitas.proxy handler must be reachable only through
    the gate. /health is the one deliberate exception: it touches no upstream and
    carries no credential.
    """
    ungated = [
        route.path for route in _proxy_handler_routes()
        if server._proxy_gate_disposition(route.path, route.path.encode()) != PROTECTED
    ]
    assert ungated == [], f"proxy routes reachable without authentication: {ungated}"


def test_gate_literals_are_exactly_the_thirteen_literal_proxy_routes():
    routed = {route.path for route in _literal_routes()}
    assert routed == server._PROXY_PATHS
    assert len(server._PROXY_PATHS) == 13


def test_every_parameterised_proxy_route_sits_under_a_declared_prefix():
    """The other half of the coextensivity claim.

    A parameterised route cannot appear in _PROXY_PATHS by construction, so the
    only thing standing between it and an unauthenticated handler is that its
    path literally begins with one of the declared prefixes. Assert that
    directly, not just that the disposition happens to come back PROTECTED --
    a future route like /vertex/... would satisfy neither, and this is the test
    that says so.
    """
    unprefixed = [route.path for route in _parameterised_routes()
                  if not route.path.startswith(server._PROXY_PATH_PREFIXES)]
    assert unprefixed == [], (
        f"parameterised proxy routes outside every gate prefix: {unprefixed}")


def test_prefixes_are_whole_segment_literals():
    for prefix in server._PROXY_PATH_PREFIXES:
        assert prefix.startswith("/") and prefix.endswith("/")
        assert "//" not in prefix
        assert "%" not in prefix and "*" not in prefix and ".." not in prefix


# --------------------------------------------------------------------------
# 2. The 13 literals are byte-identical.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(server._PROXY_PATHS))
def test_each_literal_is_still_protected(path):
    assert disposition(path) == PROTECTED
    # ...including with no raw_path at all, as a hand-built ASGI scope has.
    assert server._proxy_gate_disposition(path, None) == PROTECTED


@pytest.mark.parametrize("path", [
    "/v1/usage",            # the non-billable reporting endpoint
    "/v1/messages/",        # trailing slash: not a literal, not routed
    "/v1/Messages",         # Starlette routing is case sensitive
    "/v1//messages",
    "/health",
    "/api/billing/status",
    "/",
    "/openai/v1/chat/completions/extra",
])
def test_non_proxy_paths_still_pass_straight_through(path):
    assert disposition(path) == OPEN


# --------------------------------------------------------------------------
# 3. The prefix lane is gated.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    BEDROCK_PATH,
    "/bedrock/model/us.anthropic.claude-opus-5-v1:0/invoke-with-response-stream",
    "/bedrock/model/anthropic.claude-haiku-4-5-20251001-v1:0/invoke",
    "/bedrock/model/eu.anthropic.claude-opus-5-v1:0/invoke",  # unpriced, still gated
    "/bedrock/anything/at/all",  # wider than the router, on purpose
])
def test_bedrock_lane_is_protected(path):
    assert disposition(path) == PROTECTED


@pytest.mark.parametrize("path", [
    AZURE_PATH,
    "/azure/contoso/openai/deployments/prod-cheap-1/chat/completions",
    "/azure/a/openai/deployments/b/chat/completions",
    "/azure/contoso/openai/responses",          # refused by the handler, still gated
    "/azure/contoso/openai/v1/responses",
    "/azure/anything/at/all",                   # wider than the router, on purpose
])
def test_azure_lane_is_protected(path):
    assert disposition(path) == PROTECTED


def test_azure_lane_is_protected_before_the_query_string_is_read():
    """api-version rides in the query, and the gate must not depend on it.

    raw_path from a real ASGI server excludes the query string, but a server that
    includes it must not change the verdict either -- the gate splits on "?" for
    exactly this reason, and a request that gated differently with and without
    its query would be a bypass primitive.
    """
    raw = (AZURE_PATH + "?api-version=2024-10-21").encode()
    assert server._proxy_gate_disposition(AZURE_PATH, raw) == PROTECTED


def test_percent_encoded_colon_from_a_real_sdk_is_still_protected():
    """botocore percent-encodes ":" in the model id; unreserved chars never encode.

    The decoded path is what the router matches, so this must gate -- refusing it
    would drop billable traffic for a well-formed request.
    """
    raw = b"/bedrock/model/us.anthropic.claude-opus-5-v1%3A0/invoke"
    assert server._proxy_gate_disposition(BEDROCK_PATH, raw) == PROTECTED


def test_obfuscated_prefix_that_still_routes_is_protected_not_skipped():
    """`%62` is just "b". The router sees the decoded path, so the gate must too.

    This is the direction that matters: an encoding trick may never turn the gate
    OFF for a path the router will happily serve.
    """
    raw = b"/%62edrock/model/x/invoke"
    assert server._proxy_gate_disposition("/bedrock/model/x/invoke", raw) == PROTECTED


# --------------------------------------------------------------------------
# 4. Bypass attempts. None of these may come back OPEN.
# --------------------------------------------------------------------------

BYPASS_ATTEMPTS = [
    # (label, decoded path as ASGI would report it, raw bytes on the wire)
    ("encoded separator in the model id",
     "/bedrock/model/a/b/invoke", b"/bedrock/model/a%2Fb/invoke"),
    ("encoded traversal out of the lane",
     "/bedrock/model/../../admin/invoke", b"/bedrock/model/..%2f..%2fadmin/invoke"),
    ("uppercase encoded separator",
     "/bedrock/model/a/b/invoke", b"/bedrock/model/a%2Fb/invoke"),
    ("plain traversal inside the lane",
     "/bedrock/model/x/../../v1/messages", b"/bedrock/model/x/../../v1/messages"),
    ("traversal into the lane from outside",
     "/v1/../bedrock/model/x/invoke", b"/v1/../bedrock/model/x/invoke"),
    ("double slash",
     "/bedrock//model/x/invoke", b"/bedrock//model/x/invoke"),
    ("trailing dot segment",
     "/bedrock/model/x/invoke/.", b"/bedrock/model/x/invoke/."),
    ("double encoded dot segments",
     "/bedrock/model/x%2e%2e/invoke", b"/bedrock/model/x%252e%252e/invoke"),
    ("double encoded separator",
     "/bedrock/model/x%2f/invoke", b"/bedrock/model/x%252f/invoke"),
    ("NUL truncation",
     "/bedrock/model/x\x00/invoke", b"/bedrock/model/x%00/invoke"),
    ("newline injection",
     "/bedrock/model/x\n/invoke", b"/bedrock/model/x%0a/invoke"),
    ("backslash separators",
     "/bedrock\\model\\x\\invoke", b"/bedrock%5cmodel%5cx%5cinvoke"),
    ("encoded backslash inside the lane",
     "/bedrock/model/x\\y/invoke", b"/bedrock/model/x%5cy/invoke"),
    ("raw path disagrees with the routed path",
     "/bedrock/model/x/invoke", b"/bedrock/model/y/invoke"),
    # --- Azure. The resource segment becomes a HOSTNAME, so a separator or a
    # traversal smuggled through it is the highest-value target in the build.
    ("encoded separator in the azure resource",
     "/azure/a/b/openai/deployments/d/chat/completions",
     b"/azure/a%2fb/openai/deployments/d/chat/completions"),
    ("traversal out of the azure lane",
     "/azure/../bedrock/model/x/invoke", b"/azure/..%2fbedrock/model/x/invoke"),
    ("encoded separator in the azure deployment",
     "/azure/r/openai/deployments/a/b/chat/completions",
     b"/azure/r/openai/deployments/a%2Fb/chat/completions"),
    ("double encoded azure prefix",
     "/azure/r%2e%2e/openai/deployments/d/chat/completions",
     b"/azure/r%252e%252e/openai/deployments/d/chat/completions"),
    ("azure NUL truncation",
     "/azure/r\x00/openai/deployments/d/chat/completions",
     b"/azure/r%00/openai/deployments/d/chat/completions"),
    ("azure backslash separators",
     "/azure\\r\\openai\\deployments\\d\\chat\\completions",
     b"/azure%5cr%5copenai%5cdeployments%5cd%5cchat%5ccompletions"),
    ("azure double slash",
     "/azure//openai/deployments/d/chat/completions",
     b"/azure//openai/deployments/d/chat/completions"),
]


@pytest.mark.parametrize("label,path,raw", BYPASS_ATTEMPTS,
                         ids=[case[0] for case in BYPASS_ATTEMPTS])
def test_bypass_attempts_are_refused_never_passed_through(label, path, raw):
    assert server._proxy_gate_disposition(path, raw) == REFUSED, label


@pytest.mark.parametrize("path", [
    "/bedrockish/model/x/invoke",   # prefix must match whole segments
    "/bedrock",                     # the namespace root routes nowhere
    "/Bedrock/model/x/invoke",      # routing is case sensitive, so this 404s
    "/api/bedrock/model/x/invoke",  # not anchored at the root
    "/azurely/r/openai/deployments/d/chat/completions",
    "/azure",
    "/Azure/r/openai/deployments/d/chat/completions",
    "/api/azure/r/openai/deployments/d/chat/completions",
])
def test_near_miss_paths_are_open_because_the_router_will_not_serve_them(path):
    """Wider than the router is safe; these are outside it in the other direction.

    Each is asserted against the real router below, so "open" here can never
    drift into "open AND routable".
    """
    assert disposition(path) == OPEN


# --------------------------------------------------------------------------
# 5. The property that actually matters: gate >= router.
# --------------------------------------------------------------------------

def _future_bedrock_routes():
    """The Bedrock lane's route shapes, as the handler will register them.

    The gate has to be correct BEFORE the route lands, so the property is tested
    against the route patterns rather than against whatever is registered today.
    """
    async def _endpoint(request):  # pragma: no cover - never invoked
        raise AssertionError("router must not reach a handler in this test")

    return [
        Route("/bedrock/model/{model_id:path}/invoke", _endpoint, methods=["POST"]),
        Route("/bedrock/model/{model_id:path}/invoke-with-response-stream",
              _endpoint, methods=["POST"]),
    ]


def _routes_to_a_proxy_handler(path):
    from starlette.routing import Match

    scope = {"type": "http", "method": "POST", "path": path,
             "headers": [], "root_path": ""}
    for route in _future_bedrock_routes():
        match, _child = route.matches(scope)
        if match == Match.FULL:
            return True
    for registered in _proxy_handler_routes():
        match, _child = registered.matches(scope)
        if match == Match.FULL:
            return True
    return False


ROUTER_PARITY_PATHS = [
    case[1] for case in BYPASS_ATTEMPTS
] + [
    BEDROCK_PATH,
    "/bedrock/model/x/invoke",
    "/bedrock/model/a/b/c/invoke",
    "/bedrock/model//invoke",
    "/bedrock/model/x/invoke-with-response-stream",
    "/bedrockish/model/x/invoke",
    "/Bedrock/model/x/invoke",
    "/bedrock",
    "/bedrock/",
    "/api/bedrock/model/x/invoke",
    AZURE_PATH,
    "/azure/r/openai/deployments/d/chat/completions",
    "/azure/r/openai/deployments//chat/completions",
    "/azure//openai/deployments/d/chat/completions",
    "/azure/r/openai/responses",
    "/azure/r/openai/v1/responses",
    "/azure/r/openai/deployments/a/b/chat/completions",
    "/azurely/r/openai/deployments/d/chat/completions",
    "/Azure/r/openai/deployments/d/chat/completions",
    "/azure",
    "/azure/",
    "/api/azure/r/openai/deployments/d/chat/completions",
    "/v1/messages",
    "/v1/usage",
    "/health",
    "/",
]


@pytest.mark.parametrize("path", ROUTER_PARITY_PATHS)
def test_anything_the_router_would_serve_is_gated(path):
    """If the router matches a proxy handler, the gate MUST NOT return OPEN.

    OPEN means `return await call_next(request)` -- straight to the handler with
    no key check, no proxy:invoke check, no tenant scope, no admission control,
    and the caller's own provider credential forwarded upstream.
    """
    if _routes_to_a_proxy_handler(path):
        assert server._proxy_gate_disposition(path, path.encode()) in (PROTECTED, REFUSED), (
            f"{path!r} routes to a proxy handler but skips authentication"
        )


# --------------------------------------------------------------------------
# 6. Middleware behaviour, end to end.
# --------------------------------------------------------------------------

def _scope(path, headers=(), raw_path=None):
    return {
        "type": "http", "method": "POST", "path": path,
        "raw_path": path.encode() if raw_path is None else raw_path,
        "headers": list(headers), "client": ("127.0.0.1", 1), "query_string": b"",
        "server": ("test", 80), "scheme": "http", "root_path": "",
    }


async def _body():
    return {"type": "http.request", "body": b"{}", "more_body": False}


def _run(path, headers=(), raw_path=None, call_next=None):
    request = Request(_scope(path, headers, raw_path), _body)

    async def _forbidden(_request):
        raise AssertionError("request reached the application without the gate")

    return asyncio.run(server._protect_model_proxy(request, call_next or _forbidden))


def test_refused_path_never_reaches_the_application(monkeypatch):
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    response = _run("/bedrock/model/a/b/invoke", raw_path=b"/bedrock/model/a%2Fb/invoke")
    assert response.status_code == 400


def test_refused_path_is_refused_even_with_a_valid_key(monkeypatch):
    """The dangerous version of the previous test.

    A keyless request stops at the 401 anyway. An attacker with a perfectly good
    key is the one who gets to smuggle a crafted path through to the handler --
    and the handler builds the upstream URL from that path.
    """
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    monkeypatch.setattr(server, "_auth_context_for_key", lambda *_a: server.AuthContext(
        key_hash="opaque", organization_id="org_1", customer_id="cus_1",
        scopes=frozenset({"proxy:invoke"}),
    ))

    class Limiter:
        async def acquire(self, *_a, **_kw):  # pragma: no cover - must not run
            raise AssertionError("a refused path must not consume admission")

    monkeypatch.setattr(server, "_distributed_limiter", Limiter())

    for path, raw in [
        ("/bedrock/model/a/b/invoke", b"/bedrock/model/a%2Fb/invoke"),
        ("/bedrock/model/../../admin/invoke", b"/bedrock/model/..%2f..%2fadmin/invoke"),
        ("/bedrock/model/x\x00/invoke", b"/bedrock/model/x%00/invoke"),
    ]:
        response = _run(path, headers=[(b"x-brevitas-key", b"bvt_test")], raw_path=raw)
        assert response.status_code == 400, path


def test_bedrock_path_without_a_key_is_rejected_like_the_literals(monkeypatch):
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    assert _run(BEDROCK_PATH).status_code == 401
    assert _run("/v1/messages").status_code == 401


def test_bedrock_path_requires_the_proxy_invoke_scope(monkeypatch):
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    monkeypatch.setattr(server, "_auth_context_for_key", lambda *_a: server.AuthContext(
        key_hash="opaque", organization_id="org_1", customer_id="cus_1",
        scopes=frozenset({"usage:write"}),
    ))
    response = _run(BEDROCK_PATH, headers=[(b"x-brevitas-key", b"bvt_test")])
    assert response.status_code == 403


def test_bedrock_path_gets_tenant_scope_cache_flags_and_admission(monkeypatch):
    """A prefix match must get the WHOLE middleware, not a lighter version of it."""
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    seen = {}

    class Store:
        def cache_enabled(self, *_args):
            return True

    class Limiter:
        async def acquire(self, identity, **_kwargs):
            seen["identity"] = identity
            return SimpleNamespace(allowed=True, _limiter=object(), retry_after=0,
                                   reason="", remaining_requests=7, reset_seconds=11)

    monkeypatch.setattr(server, "_store", Store())
    monkeypatch.setattr(server, "_distributed_limiter", Limiter())
    monkeypatch.setattr(server, "_warm_enabled_cached", lambda *_a: True)
    monkeypatch.setattr(server, "_cache_enabled_cached", lambda *_a: True)
    monkeypatch.setattr(server, "_auth_context_for_key", lambda *_a: server.AuthContext(
        key_hash="opaque", organization_id="org_1", customer_id="cus_1",
        scopes=frozenset({"proxy:invoke"}),
    ))
    monkeypatch.setattr(server, "hosted_runtime", lambda: False)

    scope_bound = {}
    monkeypatch.setattr(server, "bind_circuit_scope",
                        lambda value: scope_bound.setdefault("org", value))

    captured = {}

    async def call_next(request):
        captured["organization_id"] = request.state.brevitas_organization_id
        captured["customer_id"] = request.state.brevitas_customer_id
        captured["cache_enabled"] = request.state.brevitas_cache_enabled
        captured["warm_enabled"] = request.state.brevitas_warm_enabled
        captured["auth_context"] = server._proxy_auth_context.get()
        captured["cancellation"] = request.state.brevitas_admission_cancellation

        async def stream():
            yield b"ok"

        return SimpleNamespace(headers={}, body_iterator=stream())

    _run(BEDROCK_PATH, headers=[(b"x-brevitas-key", b"bvt_test")], call_next=call_next)

    assert captured["organization_id"] == "org_1"
    assert captured["customer_id"] == "cus_1"
    assert captured["cache_enabled"] is True
    assert captured["warm_enabled"] is True
    assert captured["auth_context"] is not None       # the receipt bridge's fast path
    assert captured["cancellation"] is not None       # cooperative admission stop
    assert scope_bound["org"] == "org_1"              # per-tenant circuit, not shared
    assert seen["identity"].provider == "bedrock"     # its own global rate bucket


def test_bedrock_lane_gets_its_own_provider_bucket():
    assert server._provider_bucket(BEDROCK_PATH, b"{}") == "bedrock"
    assert server._provider_bucket("/v1/messages", b"{}") == "anthropic"
    assert server._provider_bucket("/v1/chat/completions", b'{"model":"gpt-4o"}') == "openai"


def test_azure_path_without_a_key_is_rejected_like_the_literals(monkeypatch):
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    assert _run(AZURE_PATH).status_code == 401


def test_azure_path_requires_the_proxy_invoke_scope(monkeypatch):
    monkeypatch.setenv("BREVITAS_PROXY_AUTH", "true")
    monkeypatch.setattr(server, "_auth_context_for_key", lambda *_a: server.AuthContext(
        key_hash="opaque", organization_id="org_1", customer_id="cus_1",
        scopes=frozenset({"usage:write"}),
    ))
    response = _run(AZURE_PATH, headers=[(b"x-brevitas-key", b"bvt_test")])
    assert response.status_code == 403


def test_azure_lane_gets_its_own_provider_bucket():
    """Not "openai".

    body["model"] on Azure is the customer's DEPLOYMENT name, so the body sniff
    would bucket Azure traffic by a string the customer chose -- usually landing
    on "openai" and sharing a GLOBAL rpm/concurrency bucket with first-party
    OpenAI traffic, which is a different provider with a different quota.
    """
    assert server._provider_bucket(AZURE_PATH, b'{"model":"gpt-4.1"}') == "azure_openai"
    assert server._provider_bucket(
        "/azure/r/openai/responses", b"{}") == "azure_openai"
