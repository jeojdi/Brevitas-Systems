"""The Azure OpenAI lane: where the bytes go, and what may honestly be priced.

Four failure classes, in descending order of how much they cost:

  * A CREDENTIAL LEAVES FOR THE WRONG PLACE. The resource label arrives in the
    URL path and becomes a HOSTNAME, and the caller's own Azure key rides along.
    That makes this the second Brevitas route (after Bedrock) that builds a
    destination host out of request input, and the only one where a single path
    segment decides the whole host.

  * BREVITAS'S OWN CREDENTIAL LEAVES. `x-brevitas-key` is the tenant secret; the
    forward set is an allowlist precisely so it cannot reach Azure.

  * THE REQUEST NEVER ARRIVES. `api-version` is mandatory on Azure's deployments
    data plane and lives in the query string -- which no Brevitas route had ever
    forwarded. Lose it and every request 404s at Azure while the lane looks wired
    up.

  * THE ROW BILLS WRONG. On Azure the request names a DEPLOYMENT, not a model,
    and the deployment name is whatever the customer typed. Pricing off it would
    charge a percentage of a made-up cost the moment someone names a deployment
    `gpt-4.1`. And the deployment TYPE (Global Standard vs DataZone vs
    Provisioned/PTU) changes the per-token price while appearing in no response
    field at all, so an undeclared SKU has to bill nothing rather than guess.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.routing import Match

import brevitas.proxy as proxy
from brevitas.azure import (
    AZURE_UNPRICED_PREFIX,
    AzureStreamModelSniffer,
    azure_chat_endpoint,
    azure_priced_model,
    azure_provider_label,
    azure_stream_usage_supported,
    azure_unpriced_model,
    normalize_azure_api_version,
    normalize_azure_resource,
    normalize_azure_sku,
    valid_azure_deployment,
)
from brevitas.receipts import MODEL_PRICES, TokenReceipt, calculate_costs, model_price

RESOURCE = "contoso-openai"
DEPLOYMENT = "gpt-4.1"           # the commonest Azure naming convention
API_VERSION = "2024-10-21"
PATH = f"/azure/{RESOURCE}/openai/deployments/{DEPLOYMENT}/chat/completions"
QUERY = f"api-version={API_VERSION}".encode()
KEY = "azure-resource-key-value"
BODY = {"model": DEPLOYMENT, "messages": [{"role": "user", "content": "hello"}]}

#: The dated snapshot Azure Chat Completions returns in body.model. This, and
#: only this, is what the lane is allowed to price against.
SNAPSHOT = "gpt-4.1-2025-04-14"
PARITY_SKU = "GlobalStandard"


# ==========================================================================
# 1. The resource label as a hostname.
# ==========================================================================

@pytest.mark.parametrize("resource", [
    "contoso-openai", "contoso", "a1", "my-eastus-2-resource", "a" * 63,
])
def test_real_resource_names_are_accepted(resource):
    assert normalize_azure_resource(resource) == resource


def test_case_and_whitespace_are_normalized_then_revalidated():
    """Azure portal values are copied by hand and arrive with case and newlines.

    Tolerating that is safe; the value is still matched against the strict
    pattern AFTER normalising, so nothing whitespace- or case-delimited can
    survive into a hostname.
    """
    assert normalize_azure_resource("  Contoso-OpenAI\n") == "contoso-openai"


@pytest.mark.parametrize("hostile", [
    "",
    "contoso.openai.azure.com",     # a dot ends the hostname we intended
    "contoso.evil.test",
    "https://contoso.openai.azure.com",
    "contoso/../evil",
    "contoso:443",
    "contoso@evil.test",
    "contoso#@evil.test",
    "169.254.169.254",              # the metadata endpoint, spelled plainly
    "contoso_openai",               # underscores are not legal in a DNS label
    "-contoso",                     # a label may not start with a hyphen
    "contoso-",
    "contoso evil",
    "contoso\x00",
    "a" * 64,                       # longer than a DNS label
])
def test_hostile_resource_labels_are_refused(hostile):
    assert normalize_azure_resource(hostile) == ""


@pytest.mark.parametrize("value", [
    "contoso", "  CONTOSO  ", "evil.test", "contoso/x", "", "\n\n",
    "169.254.169.254", "a" * 500, "contoso#@evil",
])
def test_a_normalized_resource_is_never_anything_but_a_label(value):
    """The property, not the cases: whatever comes back is either "" or a string
    that can only be the leftmost label of the one host suffix we build."""
    normalized = normalize_azure_resource(value)
    assert normalized == "" or (
        normalized.isascii() and normalized.islower()
        and all(char.isalnum() or char == "-" for char in normalized)
        and 1 <= len(normalized) <= 63
    )


# ==========================================================================
# 2. The deployment name as a URL segment.
# ==========================================================================

@pytest.mark.parametrize("deployment", [
    "gpt-4.1", "gpt-4.1-nano", "prod_cheap_1", "o3-mini", "a", "A" * 64,
])
def test_real_deployment_names_are_accepted(deployment):
    assert valid_azure_deployment(deployment)


def test_a_deployment_named_after_its_model_is_accepted():
    """Azure's own docs name deployments after models, dots and all.

    If the charset ever loses ".", the lane refuses the most common naming
    convention in Azure and every drop-in customer breaks at once.
    """
    assert valid_azure_deployment("gpt-4.1")
    assert valid_azure_deployment("gpt-5.6-terra")


@pytest.mark.parametrize("hostile", [
    "",
    "a/b",                      # a separator would re-target the URL
    "../../v1/messages",
    "a%2Fb",                    # already-encoded separator
    "a\\b",
    "a\x00b",
    "a\nb",
    "a b",
    ".hidden",                  # must start alphanumeric
    "..",
    "A" * 65,
])
def test_hostile_deployment_names_are_refused(hostile):
    assert not valid_azure_deployment(hostile)


# ==========================================================================
# 3. api-version, and the URL the three of them build.
# ==========================================================================

@pytest.mark.parametrize("version", [
    "2024-10-21", "2025-04-01-preview", "2024-09-01-preview", "2023-05-15",
])
def test_real_api_versions_are_accepted(version):
    assert normalize_azure_api_version(version) == version


@pytest.mark.parametrize("hostile", [
    "", "preview", "v1", "latest", "2024-13-01", "2024-00-01", "2024-10-32",
    "2024-10-21&x=1", "2024-10-21/../..", "20241021", "2024 10 21",
    "2024-10-21#frag", "2024-10-21-PREVIEW-evil", "2024-10-21\n../..",
])
def test_malformed_api_versions_are_refused(hostile):
    assert normalize_azure_api_version(hostile) == ""


def test_surrounding_whitespace_is_stripped_then_revalidated():
    """Tolerating a stray space or newline from a config file is safe: the value
    is matched against the strict pattern AFTER stripping, so nothing
    whitespace-delimited can survive into the query string."""
    assert normalize_azure_api_version("  2024-10-21\n") == "2024-10-21"
    assert normalize_azure_api_version("2024-10-21 evil") == ""


def test_the_endpoint_is_the_documented_azure_url():
    assert azure_chat_endpoint(RESOURCE, DEPLOYMENT, API_VERSION) == (
        "https://contoso-openai.openai.azure.com"
        "/openai/deployments/gpt-4.1/chat/completions?api-version=2024-10-21")


@pytest.mark.parametrize("resource,deployment,version", [
    ("evil.test", DEPLOYMENT, API_VERSION),
    ("contoso/../evil", DEPLOYMENT, API_VERSION),
    ("", DEPLOYMENT, API_VERSION),
    (RESOURCE, "a/b", API_VERSION),
    (RESOURCE, "", API_VERSION),
    (RESOURCE, DEPLOYMENT, ""),
    (RESOURCE, DEPLOYMENT, "2024-10-21&api-version=evil"),
])
def test_the_url_builder_raises_rather_than_repairing(resource, deployment, version):
    """No default resource, no stripped deployment, no assumed api-version.

    A silent repair here would send the caller's Azure credential somewhere they
    did not name, which is the only irreversible mistake this lane can make.
    """
    with pytest.raises(ValueError):
        azure_chat_endpoint(resource, deployment, version)


def test_the_built_host_can_only_ever_be_an_azure_openai_host():
    from urllib.parse import urlsplit

    for resource in ("contoso", "a1", "x" * 63):
        host = urlsplit(azure_chat_endpoint(resource, DEPLOYMENT, API_VERSION)).netloc
        assert host.endswith(".openai.azure.com")
        assert host.count(".") == 3 and "@" not in host and ":" not in host


# ==========================================================================
# 4. The SKU, and the pricing asymmetry it protects.
# ==========================================================================

@pytest.mark.parametrize("declared,canonical", [
    ("GlobalStandard", "global-standard"),
    ("global-standard", "global-standard"),
    ("Global Standard", "global-standard"),
    ("global_standard", "global-standard"),
    ("DataZoneStandard", "datazone-standard"),
    ("Standard", "standard"),
    ("ProvisionedManaged", "provisioned"),
    ("PTU", "provisioned"),
    ("GlobalBatch", "global-batch"),
])
def test_the_declared_sku_is_canonicalized(declared, canonical):
    assert normalize_azure_sku(declared) == canonical


@pytest.mark.parametrize("unknown", ["", "gold", "global standrd", "  ", "openai"])
def test_an_unrecognized_sku_names_nothing(unknown):
    assert normalize_azure_sku(unknown) == ""


def test_only_the_parity_sku_prices():
    assert azure_priced_model(SNAPSHOT, PARITY_SKU) == SNAPSHOT
    for other in ("DataZoneStandard", "Standard", "GlobalBatch", "", "gold"):
        assert azure_priced_model(SNAPSHOT, other) == "", other


def test_a_ptu_customer_bills_zero_rather_than_a_percentage_of_a_token_price():
    """Provisioned capacity is bought by the hour and has NO marginal per-token
    price, so there is no token cost to take a percentage of. Billing one would
    be inventing the customer's spend."""
    assert azure_priced_model(SNAPSHOT, "ProvisionedManaged") == ""
    receipt = TokenReceipt(fresh_input_tokens=1_000_000, output_tokens=1_000_000)
    model = azure_unpriced_model(DEPLOYMENT)
    costs = calculate_costs(azure_provider_label(), model, 1_000_000, receipt)
    assert costs["pricing_status"] == "unpriced"
    assert costs["measured_savings_usd"] is None


def test_an_undeclared_sku_is_unpriced_not_assumed_cheapest():
    assert azure_priced_model(SNAPSHOT, "") == ""


def test_the_priced_path_actually_resolves_a_price_row():
    """The other direction: gating must not have made the lane un-billable.

    A gate that refuses everything is as broken as one that guesses -- it just
    fails quietly, in Brevitas's own pocket.
    """
    model = azure_priced_model(SNAPSHOT, PARITY_SKU)
    assert model
    costs = calculate_costs(azure_provider_label(), model, 1_000,
                            TokenReceipt(fresh_input_tokens=1_000, output_tokens=10))
    assert costs["pricing_status"] == "priced"
    # And it resolves through the OpenAI table, which is the parity claim the
    # SKU gate exists to qualify (brevitas/receipts.py canonical_provider).
    assert model_price(azure_provider_label(), model) == model_price("openai", "gpt-4.1")


def test_an_unrecognized_response_model_falls_to_unpriced_never_to_a_guess():
    """Azure's REST reference describes `model` generically and warns that
    response objects can change, so the value is high-confidence, not
    contractual. Absence must cost the price, not produce one."""
    for value in ("", "   ", "a" * 129, "not a model at all!", "../../etc/passwd"):
        assert azure_priced_model(value, PARITY_SKU) == ""


# -- the unpriced marker ----------------------------------------------------

def test_the_unpriced_marker_can_never_match_any_price_row():
    """The load-bearing property of AZURE_UNPRICED_PREFIX.

    Reporting the deployment name bare would price a deployment somebody named
    "gpt-4.1" at gpt-4.1 rates -- a fee taken as a percentage of a cost we
    invented. Assert against EVERY row in the table, including the prefix
    fallback that resolves dated snapshots, so a new row can never quietly make
    the marker priceable.
    """
    for provider, model in MODEL_PRICES:
        marker = azure_unpriced_model(model)
        assert model_price(azure_provider_label(), marker) is None, (provider, model)
        assert model_price(provider, marker) is None, (provider, model)
        assert model_price("openai", marker) is None, (provider, model)


def test_the_unpriced_marker_says_why_it_earned_nothing():
    assert azure_unpriced_model(DEPLOYMENT) == f"{AZURE_UNPRICED_PREFIX}{DEPLOYMENT}"


def test_an_unsafe_deployment_name_never_reaches_a_receipt_verbatim():
    """The receipt model is validated on the way out too.

    api/server.py's UsageReportRequest.model rejects control characters and caps
    at 128; a value that raises inside _hosted_proxy_receipt is caught, logged
    once, and DROPS the billable row -- so an unreadable name must cost the
    price, never the whole receipt.
    """
    from api.server import UsageReportRequest

    assert UsageReportRequest.model_fields["model"].metadata[0].max_length == 128
    for hostile in ("a\nb", "a" * 500, "../../x", ""):
        marker = azure_unpriced_model(hostile)
        assert marker == f"{AZURE_UNPRICED_PREFIX}unknown"
        assert len(marker) <= 128
        assert not any(ord(char) < 32 or ord(char) == 127 for char in marker)


# ==========================================================================
# 5. Streaming: the model on chunk 1, and usage only where it can be asked for.
# ==========================================================================

@pytest.mark.parametrize("version,supported", [
    ("2024-08-01-preview", False),   # predates stream_options on Azure
    ("2024-06-01", False),
    ("2024-09-01-preview", True),    # the version that added it
    ("2024-10-21", True),
    ("2025-04-01-preview", True),
    ("", False),                     # unparseable fails CLOSED
    ("preview", False),
    ("nonsense", False),
])
def test_stream_usage_support_is_decided_by_the_api_version(version, supported):
    assert azure_stream_usage_supported(version) is supported


def _sse(*events: dict) -> bytes:
    return b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)


def test_the_sniffer_finds_the_snapshot_on_the_first_chunk():
    sniffer = AzureStreamModelSniffer()
    sniffer.feed(_sse({"id": "chatcmpl-1", "model": SNAPSHOT, "choices": []}))
    assert sniffer.model == SNAPSHOT


def test_the_sniffer_reassembles_frames_split_across_network_chunks():
    """SSE frame boundaries have nothing to do with TCP chunk boundaries."""
    stream = _sse({"id": "chatcmpl-1", "model": SNAPSHOT, "choices": []})
    sniffer = AzureStreamModelSniffer()
    for index in range(0, len(stream), 3):
        sniffer.feed(stream[index:index + 3])
    assert sniffer.model == SNAPSHOT


def test_the_sniffer_ignores_frames_that_name_no_model():
    sniffer = AzureStreamModelSniffer()
    sniffer.feed(b": ping\n\n")
    sniffer.feed(_sse({"choices": [], "id": "chatcmpl-1"}))
    sniffer.feed(_sse({"model": SNAPSHOT}))
    assert sniffer.model == SNAPSHOT


@pytest.mark.parametrize("hostile", [
    b"\xff\xff\xff\xff not sse at all",
    b"data: {not json}\n\n",
    b"data: [DONE]\n\n",
    b'data: {"model": 42}\n\n',
    b'data: {"model": ""}\n\n',
    b"data: []\n\n",
])
def test_a_malformed_stream_never_raises_and_never_buffers(hostile):
    """The sniffer runs inside the generator yielding the customer's response.

    Raising here would truncate a paid, in-flight completion in order to protect
    a metering read. Losing the model is the correct trade; losing the answer is
    not.
    """
    sniffer = AzureStreamModelSniffer()
    sniffer.feed(hostile)
    assert sniffer.model == ""
    for _ in range(500):
        sniffer.feed(b"x" * 1024)          # a stream that is never SSE
    assert sniffer.model == ""
    assert len(sniffer._buffer) <= sniffer._MAX_BUFFER_BYTES + 1024


# ==========================================================================
# 6. Handler behaviour.
# ==========================================================================

def _request(headers: dict[str, str], body: bytes | None = None, path: str = PATH,
             query: bytes = QUERY) -> Request:
    payload = body if body is not None else json.dumps(BODY).encode()
    scope = {
        "type": "http", "method": "POST", "path": path, "raw_path": path.encode(),
        "headers": [(name.lower().encode(), value.encode())
                    for name, value in headers.items()],
        "client": ("127.0.0.1", 1), "query_string": query, "server": ("test", 80),
        "scheme": "http", "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request(scope, receive)
    # Populated by api/server.py's _protect_model_proxy before a handler runs.
    request.state.brevitas_organization_id = "org_1"
    request.state.brevitas_customer_id = "cus_1"
    request.state.brevitas_cache_enabled = False
    return request


class _Upstream:
    def __init__(self, payload: dict | None = None, status_code: int = 200,
                 content: bytes | None = None,
                 content_type: str = "application/json"):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.content = (content if content is not None
                        else json.dumps(self._payload).encode())
        self.headers = {"content-type": content_type,
                        "apim-request-id": "azure-correlation-1"}

    def json(self):
        return self._payload

    async def aread(self):
        return self.content

    async def aclose(self):
        return None


COMPLETE_RESPONSE = {
    "id": "chatcmpl-1", "model": SNAPSHOT,
    "choices": [{"finish_reason": "stop",
                 "message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 42,
              "prompt_tokens_details": {"cached_tokens": 900}},
}


@pytest.fixture
def lane(monkeypatch):
    """Capture the upstream call and the emitted receipt without a network."""
    captured: dict = {"receipts": []}

    async def fake_provider_request(provider, operation, method, endpoint, *,
                                    headers, stream=False, json_body=None,
                                    content=None):
        captured.update(provider=provider, operation=operation, method=method,
                        endpoint=endpoint, headers=headers, stream=stream,
                        json_body=json_body, content=content)
        return captured.get("response") or _Upstream(COMPLETE_RESPONSE)

    async def reporter(key, payload):
        captured["receipts"].append(payload)

    monkeypatch.setattr(proxy, "_provider_request", fake_provider_request)
    monkeypatch.setattr(proxy, "_usage_reporter", reporter)
    monkeypatch.delenv("BREVITAS_AZURE_SKU", raising=False)
    return captured


def _call(request, resource: str = RESOURCE, deployment: str = DEPLOYMENT):
    return asyncio.run(proxy.proxy_azure_chat(request, resource, deployment))


def _headers(**extra: str) -> dict[str, str]:
    return {"api-key": KEY, proxy.AZURE_SKU_HEADER: PARITY_SKU, **extra}


def _only_receipt(lane):
    assert len(lane["receipts"]) == 1, lane["receipts"]
    return lane["receipts"][0]


# -- refusals ---------------------------------------------------------------

def test_a_request_with_no_credential_is_refused_before_the_body_is_read(lane):
    with pytest.raises(HTTPException) as caught:
        _call(_request({}))
    assert caught.value.status_code == 401
    assert "api-key" in caught.value.detail
    assert "endpoint" not in lane, "no upstream may be contacted without a key"


@pytest.mark.parametrize("resource", [
    "evil.test", "contoso.openai.azure.com", "169.254.169.254", "contoso@evil.test",
    "", "a" * 64, "-contoso",
])
def test_an_unsafe_resource_never_reaches_a_hostname(lane, resource):
    with pytest.raises(HTTPException) as caught:
        _call(_request(_headers()), resource=resource)
    assert caught.value.status_code == 400
    assert "endpoint" not in lane


@pytest.mark.parametrize("deployment", ["a/b", "", "a" * 65, "a b", "../.."])
def test_an_unsafe_deployment_never_reaches_a_url(lane, deployment):
    with pytest.raises(HTTPException) as caught:
        _call(_request(_headers()), deployment=deployment)
    assert caught.value.status_code == 400
    assert "endpoint" not in lane


@pytest.mark.parametrize("query,reason", [
    (b"", "api-version is mandatory on Azure's deployments data plane"),
    (b"api-version=", "an empty api-version is not a version"),
    (b"api-version=latest", "only dated versions may reach the query string"),
    (b"api-version=2024-10-21&api-version=evil", "a second value is ambiguous"),
    (b"api-version=2024-10-21&stream=true", "an unknown parameter is refused, not dropped"),
    (b"apiversion=2024-10-21", "a misspelling must not silently become nothing"),
])
def test_a_bad_query_string_is_refused_rather_than_forwarded(lane, query, reason):
    with pytest.raises(HTTPException) as caught:
        _call(_request(_headers(), query=query))
    assert caught.value.status_code == 400, reason
    assert "endpoint" not in lane, reason


def test_a_declared_but_unrecognized_sku_is_a_400_not_a_silent_zero(lane):
    """The direction that matters.

    An ABSENT sku header means "undeclared" and bills nothing on purpose. A
    PRESENT one that we cannot read is a typo, and silently unpricing every row a
    customer meant to declare is a revenue leak with no signal anywhere.
    """
    with pytest.raises(HTTPException) as caught:
        _call(_request({"api-key": KEY, proxy.AZURE_SKU_HEADER: "globl-standard"}))
    assert caught.value.status_code == 400
    assert "endpoint" not in lane


def test_the_responses_api_is_refused_with_a_reason_not_a_404(lane):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(proxy.proxy_azure_responses(_request(_headers()), RESOURCE))
    assert caught.value.status_code == 400
    assert "Responses" in caught.value.detail
    assert "deployment" in caught.value.detail.lower()
    assert "chat/completions" in caught.value.detail


# -- the happy path ---------------------------------------------------------

def test_the_request_reaches_the_documented_azure_url_with_its_query_string(lane):
    """The fix for the gap this lane exposed.

    NO Brevitas route forwarded a query parameter before this one: every handler
    builds its endpoint from a literal and drops `request.url.query`. On Azure
    that is fatal rather than cosmetic -- api-version is mandatory, and without
    it the request never reaches a deployment.
    """
    _call(_request(_headers()))
    assert lane["endpoint"] == (
        "https://contoso-openai.openai.azure.com"
        "/openai/deployments/gpt-4.1/chat/completions?api-version=2024-10-21")
    assert lane["method"] == "POST"
    # Its own circuit and its own metrics bucket, not first-party OpenAI's.
    assert lane["provider"] == "azure_openai"


def test_the_forwarded_api_version_is_re_minted_not_concatenated(lane):
    """A validated token goes into the URL, never the caller's raw bytes."""
    _call(_request(_headers(), query=b"api-version=2025-04-01-preview"))
    assert lane["endpoint"].endswith("?api-version=2025-04-01-preview")
    assert lane["endpoint"].count("?") == 1


def test_the_native_api_key_header_is_forwarded_and_nothing_else_is(lane):
    _call(_request({
        "api-key": KEY,
        proxy.AZURE_SKU_HEADER: PARITY_SKU,
        "content-type": "application/json",
        "x-ms-client-request-id": "client-1",
        "x-brevitas-key": "bvt_our_own_tenant_secret",
        "cookie": "session=abc",
        "openai-organization": "org-leak",
        "host": "api.brevitassystems.com",
    }))
    forwarded = {name.lower(): value for name, value in lane["headers"].items()}
    assert forwarded["api-key"] == KEY
    assert forwarded["x-ms-client-request-id"] == "client-1"
    for leaked in ("x-brevitas-key", proxy.AZURE_SKU_HEADER, "cookie",
                   "openai-organization", "host"):
        assert leaked not in forwarded, (
            f"{leaked} reached Azure. Our own tenant credential must never leave "
            "the building, and a Brevitas-only control header is not Azure's "
            "business"
        )


def test_an_entra_bearer_is_forwarded_when_there_is_no_api_key(lane):
    """Managed identity and Entra ID callers send Authorization, not api-key."""
    bearer = "Bearer entra-access-token"
    _call(_request({"authorization": bearer, proxy.AZURE_SKU_HEADER: PARITY_SKU}))
    forwarded = {name.lower(): value for name, value in lane["headers"].items()}
    assert forwarded["authorization"] == bearer
    assert "api-key" not in forwarded


def test_content_type_is_sent_exactly_once(lane):
    """A dict is not case-insensitive.

    Seeding {"Content-Type": ...} and then copying the caller's "content-type"
    over it leaves BOTH keys, and httpx puts two Content-Type headers on the
    wire -- which Azure answers with an opaque 400 that looks like a body bug.
    """
    _call(_request(_headers(**{"content-type": "application/json"})))
    names = [name.lower() for name in lane["headers"]]
    assert names.count("content-type") == 1


def test_content_type_is_defaulted_when_the_caller_omits_it(lane):
    _call(_request(_headers()))
    forwarded = {name.lower(): value for name, value in lane["headers"].items()}
    assert forwarded["content-type"] == "application/json"


def test_the_body_is_forwarded_byte_for_byte(lane):
    """A passthrough lane must not re-serialize: unknown fields and key order are
    the caller's, and a round-trip is a silent way to drop one."""
    raw = (b'{"model":"gpt-4.1","messages":[{"role":"user","content":"hi"}],'
           b'"an_unknown_future_field":{"z":1,"a":2}}')
    _call(_request(_headers(), body=raw))
    assert lane["content"] == raw
    assert lane["json_body"] is None


def test_no_azure_credential_is_ever_stored(lane, monkeypatch):
    """The lane's central claim. The key may appear in exactly one place: the
    outbound api-key header."""
    stored: list = []
    monkeypatch.setattr(proxy, "_cache_store",
                        lambda *args, **kwargs: stored.append(args))
    _call(_request(_headers()))
    assert stored == []                                   # cache is off by default
    for receipt in lane["receipts"]:
        assert KEY not in json.dumps(receipt, default=str)


def test_the_azure_correlation_header_survives_to_the_caller(lane):
    """apim-request-id is the ONE response header Azure documents. Stripping it
    leaves an Azure customer with nothing to quote in a Microsoft support case."""
    response = _call(_request(_headers()))
    assert response.headers["apim-request-id"] == "azure-correlation-1"


# -- the receipt ------------------------------------------------------------

def test_the_receipt_prices_the_response_model_not_the_deployment_name(lane):
    """The whole pricing story of this lane, in one assertion.

    The deployment here is literally named "gpt-4.1" while the model that
    answered is gpt-4.1-2025-04-14. Both resolve to a price, so a lane that
    priced the deployment name would look correct in every test that did not
    separate them -- and would be inventing the customer's cost the first time
    somebody's "gpt-4.1" deployment was actually serving something else.
    """
    _call(_request(_headers()))
    receipt = _only_receipt(lane)

    assert receipt["model"] == SNAPSHOT
    assert receipt["model"] != DEPLOYMENT
    assert receipt["provider"] == "azure_openai", (
        "recording 'openai' would erase the route and make a future "
        "('azure_openai', model) price row unreachable")
    assert receipt["fresh_input_tokens"] == 100
    assert receipt["cached_input_tokens"] == 900
    assert receipt["output_tokens"] == 42
    assert receipt["receipt_available"] is True
    assert receipt["receipt_source"] == "proxy"
    # The dedupe key must be server-minted, never borrowed from a caller header.
    assert receipt["request_id"].startswith(proxy.RECEIPT_ID_PREFIX)

    costs = calculate_costs(receipt["provider"], receipt["model"],
                            receipt["baseline_tokens"],
                            TokenReceipt(fresh_input_tokens=100,
                                         cached_input_tokens=900,
                                         output_tokens=42))
    assert costs["pricing_status"] == "priced"


def test_an_undeclared_sku_records_the_row_but_earns_nothing(lane):
    """PTU and everything else we cannot establish: a row, honestly unpriced."""
    _call(_request({"api-key": KEY}))
    receipt = _only_receipt(lane)
    assert receipt["model"] == f"{AZURE_UNPRICED_PREFIX}{DEPLOYMENT}"
    assert receipt["receipt_available"] is True
    assert calculate_costs(receipt["provider"], receipt["model"], 1_000,
                           TokenReceipt(fresh_input_tokens=1_000)
                           )["pricing_status"] == "unpriced"


@pytest.mark.parametrize("sku", ["DataZoneStandard", "Standard", "ProvisionedManaged"])
def test_a_non_parity_sku_records_the_row_but_earns_nothing(lane, sku):
    _call(_request({"api-key": KEY, proxy.AZURE_SKU_HEADER: sku}))
    receipt = _only_receipt(lane)
    assert receipt["model"] == f"{AZURE_UNPRICED_PREFIX}{DEPLOYMENT}"
    assert calculate_costs(receipt["provider"], receipt["model"], 1_000,
                           TokenReceipt(fresh_input_tokens=1_000)
                           )["pricing_status"] == "unpriced"


def test_the_env_default_sku_applies_when_the_caller_declares_nothing(lane, monkeypatch):
    monkeypatch.setenv("BREVITAS_AZURE_SKU", "GlobalStandard")
    _call(_request({"api-key": KEY}))
    assert _only_receipt(lane)["model"] == SNAPSHOT


def test_a_typo_in_the_env_default_bills_zero_rather_than_400ing_the_fleet(lane, monkeypatch):
    """The opposite call from the header, and deliberately so: a per-request
    header typo affects one caller, an env typo affects every request on the
    box."""
    monkeypatch.setenv("BREVITAS_AZURE_SKU", "globl-standard")
    _call(_request({"api-key": KEY}))
    assert _only_receipt(lane)["model"] == f"{AZURE_UNPRICED_PREFIX}{DEPLOYMENT}"


def test_a_response_that_names_no_model_falls_to_unpriced(lane):
    """Azure's own reference does not contract `model`, and its 200 example omits
    it. Absence must cost the price, never produce one."""
    lane["response"] = _Upstream({**COMPLETE_RESPONSE, "model": None})
    _call(_request(_headers()))
    assert _only_receipt(lane)["model"] == f"{AZURE_UNPRICED_PREFIX}{DEPLOYMENT}"


def test_a_response_that_echoes_the_deployment_name_is_not_priced_as_a_model(lane):
    """The Responses-API failure mode, appearing on chat completions.

    If Azure ever answers with the deployment alias, the row must resolve
    through whatever that alias is worth in MODEL_PRICES -- which, for a
    deployment named `prod-cheap-1`, is nothing.
    """
    lane["response"] = _Upstream({**COMPLETE_RESPONSE, "model": "prod-cheap-1"})
    _call(_request(_headers()), deployment="prod-cheap-1")
    receipt = _only_receipt(lane)
    assert calculate_costs(receipt["provider"], receipt["model"], 1_000,
                           TokenReceipt(fresh_input_tokens=1_000)
                           )["pricing_status"] == "unpriced"


def test_a_passthrough_row_reports_exactly_zero_transformation_delta(lane):
    """Honest by construction: the lane injects nothing, so it claims nothing.

    compressed_tokens == baseline_tokens is the only truthful pair for a lane
    that forwards the caller's bytes unchanged, and cache_attributable stays
    False so the customer's own cache activity never enters the billable number.
    """
    _call(_request(_headers()))
    receipt = _only_receipt(lane)
    assert receipt["compressed_tokens"] == receipt["baseline_tokens"]
    assert receipt["cache_attributable"] is False
    assert receipt["strategy"] == "passthrough"


def test_a_failed_upstream_writes_no_receipt(lane):
    lane["response"] = _Upstream({"error": {"code": "401"}}, status_code=401)
    response = _call(_request(_headers()))
    assert response.status_code == 401
    assert lane["receipts"] == []


def test_the_lane_never_originates_a_warming_ping(lane, monkeypatch):
    """api/worker.py refuses any prefix whose recorded upstream differs from its
    provider's single spec endpoint, and WARM_PROVIDER_SPECS has no Azure entry
    at all -- so an Azure observation would be stored, claimed, and permanently
    marked prefix_invalid. Recording work guaranteed to be discarded is worse
    than not recording it."""
    observed = []
    monkeypatch.setattr(proxy, "_observe_warm_prefix",
                        lambda *args, **kwargs: observed.append(args))
    _call(_request(_headers()))
    assert observed == []


# -- streaming --------------------------------------------------------------

STREAM_BYTES = (
    b'data: {"id":"chatcmpl-1","model":"gpt-4.1-2025-04-14","choices":'
    b'[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-1","model":"gpt-4.1-2025-04-14","choices":[],'
    b'"usage":{"prompt_tokens":1000,"completion_tokens":42,'
    b'"prompt_tokens_details":{"cached_tokens":900}}}\n\n'
    b"data: [DONE]\n\n"
)


def _stream(lane, monkeypatch, headers=None, query=QUERY, body=None,
            upstream_bytes=None):
    wire = STREAM_BYTES if upstream_bytes is None else upstream_bytes
    lane["response"] = _Upstream(content=wire, content_type="text/event-stream")

    class _Http:
        async def iter_bytes(self, provider, response):
            lane["stream_provider"] = provider
            for index in range(0, len(wire), 7):
                yield wire[index:index + 7]

    monkeypatch.setattr(proxy, "provider_http", _Http())
    payload = body if body is not None else json.dumps({**BODY, "stream": True}).encode()
    response = _call(_request(headers if headers is not None else _headers(),
                              body=payload, query=query))

    async def drain():
        return b"".join([chunk async for chunk in response.body_iterator])

    return response, asyncio.run(drain())


def test_a_streamed_call_prices_the_model_from_the_first_chunk(lane, monkeypatch):
    """Azure puts the snapshot on EVERY chunk, so the model is known from chunk 1
    -- long before the usage chunk that may never arrive."""
    response, drained = _stream(lane, monkeypatch)
    assert drained == STREAM_BYTES, "response bytes must be verbatim"
    assert lane["stream"] is True
    assert lane["stream_provider"] == "azure_openai"

    receipt = _only_receipt(lane)
    assert receipt["provider"] == "azure_openai"
    assert receipt["model"] == SNAPSHOT
    assert receipt["fresh_input_tokens"] == 100
    assert receipt["cached_input_tokens"] == 900
    assert receipt["output_tokens"] == 42
    assert receipt["receipt_available"] is True


def test_stream_usage_is_requested_only_where_the_api_version_understands_it(lane, monkeypatch):
    """Injecting stream_options into an api-version that predates it is not a
    missed optimisation, it is a 400 on a paying customer's request."""
    _stream(lane, monkeypatch, query=b"api-version=2024-10-21")
    assert lane["json_body"]["stream_options"] == {"include_usage": True}
    assert lane["content"] is None


def test_an_old_api_version_is_forwarded_byte_verbatim_instead(lane, monkeypatch):
    raw = json.dumps({**BODY, "stream": True}).encode()
    _stream(lane, monkeypatch, query=b"api-version=2024-06-01", body=raw)
    assert lane["content"] == raw, (
        "an api-version that cannot take stream_options must receive the "
        "caller's bytes unchanged, not a re-serialized body with a parameter it "
        "will reject")
    assert lane["json_body"] is None


def test_a_caller_supplied_stream_options_is_never_overwritten(lane, monkeypatch):
    raw = json.dumps({**BODY, "stream": True,
                      "stream_options": {"include_usage": False}}).encode()
    _stream(lane, monkeypatch, body=raw)
    assert lane["content"] == raw
    assert lane["json_body"] is None


def test_a_corrupt_stream_still_delivers_every_byte_to_the_caller(lane, monkeypatch):
    """Metering degrades; the customer's answer does not."""
    garbage = b"\xff\xff\xff\xff garbage that is not an event stream"
    _response, drained = _stream(lane, monkeypatch, upstream_bytes=garbage)
    assert drained == garbage
    receipt = _only_receipt(lane)
    # Loud rather than silent: a missing receipt is labelled, not zero-filled,
    # and an unreadable stream names no model so the row cannot be priced.
    assert receipt["receipt_available"] is False
    assert receipt["strategy"].endswith(":missing_receipt")
    assert receipt["model"] == f"{AZURE_UNPRICED_PREFIX}{DEPLOYMENT}"


# ==========================================================================
# 7. The response cache: where this lane earns real savings today.
# ==========================================================================
#
# A passthrough lane transforms nothing, so its transformation delta is honestly
# zero. A semantic-cache hit is different: it touches no upstream at all, so it
# is 100% savings on a byte-preserving strategy -- billable on this route exactly
# as on every other. That makes the cache plumbing revenue-critical here.

class _Hit:
    prompt_tokens = 1_000
    completion_tokens = 200
    similarity = 1.0
    kind = "exact"
    origin_request_id = "proxy:the_call_that_paid"
    response = COMPLETE_RESPONSE


class _Cache:
    def __init__(self, hit=None):
        self.hit = hit
        self.looked_up = None
        self.stored = []

    def lookup(self, body, provider, model, gate_key=""):
        self.looked_up = (provider, model)
        return self.hit

    def store(self, body, provider, model, response, *, prompt_tokens=0,
              completion_tokens=0, origin_request_id=""):
        self.stored.append({"provider": provider, "model": model, "body": body,
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens})


def _with_cache(monkeypatch, cache, headers=None):
    monkeypatch.setattr(proxy, "_get_cache", lambda: cache)
    request = _request(headers if headers is not None else _headers())
    request.state.brevitas_cache_enabled = True
    return request


def test_a_cache_hit_replays_without_touching_azure_and_prices_like_its_ancestor(
        lane, monkeypatch):
    """The replay must price off the model in the response that was PAID for.

    Reading the deployment name here instead would make every replay unpriced --
    silently discarding the highest-savings rows this lane produces, which are
    exactly the rows the fee is computed from.
    """
    cache = _Cache(hit=_Hit())
    response = _call(_with_cache(monkeypatch, cache))

    assert response.status_code == 200
    assert "endpoint" not in lane, "a hit must not reach an upstream"
    receipt = _only_receipt(lane)
    assert receipt["provider"] == "azure_openai"
    assert receipt["model"] == SNAPSHOT
    assert receipt["strategy"] == "exact_cache"
    assert receipt["cache_attributable"] is True
    # The forward link to the paid ancestor: without it the zero-spend row is
    # indistinguishable from a forged one and bills nothing.
    assert receipt["savings_anchor_request_id"] == "proxy:the_call_that_paid"


def test_a_replay_under_an_undeclared_sku_is_unpriced_like_its_ancestor(lane, monkeypatch):
    cache = _Cache(hit=_Hit())
    _call(_with_cache(monkeypatch, cache, headers={"api-key": KEY}))
    assert _only_receipt(lane)["model"] == f"{AZURE_UNPRICED_PREFIX}{DEPLOYMENT}"


def test_a_complete_azure_response_is_actually_cacheable(lane, monkeypatch):
    cache = _Cache()
    _call(_with_cache(monkeypatch, cache))
    assert len(cache.stored) == 1, "a complete response was not stored"
    entry = cache.stored[0]
    assert (entry["prompt_tokens"], entry["completion_tokens"]) == (1000, 42)


def test_a_truncated_azure_response_is_not_cached(lane, monkeypatch):
    cache = _Cache()
    lane["response"] = _Upstream({
        **COMPLETE_RESPONSE,
        "choices": [{"finish_reason": "length",
                     "message": {"role": "assistant", "content": "hi"}}]})
    _call(_with_cache(monkeypatch, cache))
    assert cache.stored == []


def test_the_cache_namespace_pins_the_deployment_the_resource_and_the_version(
        lane, monkeypatch):
    """A cached Azure answer must never replay for first-party OpenAI, for a
    different deployment, for another tenant's resource, or across api-versions
    whose response shapes differ."""
    cache = _Cache()
    _call(_with_cache(monkeypatch, cache))
    expected = (azure_provider_label(), f"{DEPLOYMENT}@{RESOURCE}@{API_VERSION}")
    assert cache.looked_up == expected
    assert cache.stored[0]["model"] == expected[1]


def test_the_cached_body_carries_no_azure_credential(lane, monkeypatch):
    """_cache_body varies on a salted digest of the credential, never the value."""
    cache = _Cache()
    _call(_with_cache(monkeypatch, cache))
    serialized = json.dumps(cache.stored[0]["body"], default=str)
    assert KEY not in serialized


def test_a_streamed_call_never_consults_the_cache(lane, monkeypatch):
    """cacheable() already refuses streams; asserting it here keeps the lane from
    returning a non-stream JSON body to a client awaiting SSE frames."""
    cache = _Cache(hit=_Hit())
    monkeypatch.setattr(proxy, "_get_cache", lambda: cache)
    lane["response"] = _Upstream(content=STREAM_BYTES, content_type="text/event-stream")

    class _Http:
        async def iter_bytes(self, provider, response):
            yield STREAM_BYTES

    monkeypatch.setattr(proxy, "provider_http", _Http())
    request = _request(_headers(), body=json.dumps({**BODY, "stream": True}).encode())
    request.state.brevitas_cache_enabled = True
    _call(request)
    assert cache.looked_up is None


# ==========================================================================
# 8. Routing: neither path parameter can span a separator.
# ==========================================================================

def _azure_routes():
    from api.server import app

    def leaves(routes):
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from leaves(original.routes)
                continue
            nested = getattr(route, "routes", None)
            if nested:
                yield from leaves(nested)
                continue
            yield route

    return [route for route in leaves(app.routes)
            if getattr(route, "path", "").startswith("/azure/")]


def _matches(path):
    scope = {"type": "http", "method": "POST", "path": path, "headers": [],
             "root_path": ""}
    for route in _azure_routes():
        match, child = route.matches(scope)
        if match == Match.FULL:
            return child["path_params"]
    return None


def test_the_lane_registers_exactly_the_three_azure_routes():
    assert sorted(route.path for route in _azure_routes()) == [
        "/azure/{resource}/openai/deployments/{deployment}/chat/completions",
        "/azure/{resource}/openai/responses",
        "/azure/{resource}/openai/v1/responses",
    ]


def test_the_path_parameters_cannot_span_a_separator():
    """`{resource}` and `{deployment}` are `[^/]+`, not `:path`.

    A greedy converter would hand the handler "a/b" and make the URL builder the
    only thing standing between a crafted path and a re-targeted upstream host.
    The router refusing to match is a second, independent stop, and the proxy
    gate refusing an encoded separator is a third.
    """
    assert _matches(PATH) == {"resource": RESOURCE, "deployment": DEPLOYMENT}
    assert _matches("/azure/a/b/openai/deployments/d/chat/completions") is None
    assert _matches("/azure/r/openai/deployments/a/b/chat/completions") is None
    assert _matches("/azure//openai/deployments/d/chat/completions") is None
    assert _matches("/azure/r/openai/deployments//chat/completions") is None
    assert _matches(PATH + "/extra") is None


def test_the_openai_sdk_default_path_shape_is_the_one_that_routes():
    """openai-python's AzureOpenAI builds
    `{azure_endpoint}/openai/deployments/{deployment}/chat/completions`, so a
    customer setting azure_endpoint to `<gateway>/azure/<resource>` gets exactly
    this path. If it stops matching, every drop-in customer breaks silently."""
    azure_endpoint = f"https://api.brevitassystems.com/azure/{RESOURCE}"
    built = f"{azure_endpoint.rstrip('/')}/openai/deployments/{DEPLOYMENT}/chat/completions"
    assert built.endswith(PATH)
    assert _matches(PATH) is not None


def test_the_lane_and_its_helpers_still_agree_on_the_labels():
    """Pins the functions the handler depends on against its own constants."""
    assert azure_provider_label() == "azure_openai"
    assert azure_priced_model(SNAPSHOT, PARITY_SKU) == SNAPSHOT
    assert azure_unpriced_model(DEPLOYMENT).startswith(AZURE_UNPRICED_PREFIX)
