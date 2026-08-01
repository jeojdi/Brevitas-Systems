"""The Bedrock lane: where the bytes go, what goes with them, and what bills.

Three failure classes, in descending order of how much they cost:

  * A CREDENTIAL LEAVES FOR THE WRONG PLACE. The model id and the region are both
    caller-controlled and both end up inside an upstream URL, and the caller's
    own Bedrock bearer token rides along. This is the only Brevitas route that
    builds a hostname from request input.

  * BREVITAS'S OWN CREDENTIAL LEAVES. `x-brevitas-key` is the tenant secret; the
    forward set is an allowlist precisely so it cannot be forwarded to AWS.

  * THE ROW DOES NOT BILL, OR BILLS WRONG. A receipt that resolves to "unpriced"
    silently earns nothing; a receipt priced through the wrong table earns the
    wrong amount. Bedrock ids (`us.anthropic.claude-opus-5-v1:0`) match no
    MODEL_PRICES key at all, and streamed Bedrock responses put the usage object
    inside a binary event-stream frame rather than on a `data:` line -- so both
    the normalisation and the frame decoder are load-bearing for revenue.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.routing import Match

import brevitas.proxy as proxy
from brevitas.bedrock import (
    BedrockEventStreamDecoder,
    bedrock_priced_model,
    bedrock_provider_label,
    bedrock_runtime_endpoint,
    normalize_bedrock_model,
    normalize_bedrock_region,
    valid_bedrock_model_id,
)
from brevitas.receipts import TokenReceipt, calculate_costs

MODEL = "us.anthropic.claude-opus-5-v1:0"
INVOKE_PATH = f"/bedrock/model/{MODEL}/invoke"
STREAM_PATH = f"/bedrock/model/{MODEL}/invoke-with-response-stream"
BEARER = "Bearer bedrock-api-key-value"
BODY = {
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hello"}],
}


# ==========================================================================
# 1. The model id as a URL segment.
# ==========================================================================

@pytest.mark.parametrize("model_id", [
    MODEL,
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-sonnet-5-v1:0",
    "a",
    "A" * 128,
])
def test_real_model_ids_are_accepted(model_id):
    assert valid_bedrock_model_id(model_id)


@pytest.mark.parametrize("hostile", [
    "",
    "a/b",                      # a path separator would re-target the URL
    "../../v1/messages",
    "a%2Fb",                    # already-encoded separator
    "a\\b",
    "a\x00b",
    "a\nb",
    "a b",
    ".hidden",                  # must start alphanumeric
    "A" * 129,                  # > UsageReportRequest.model max_length
    "arn:aws:bedrock:us-east-1:1:inference-profile/us.anthropic.claude-opus-5-v1:0",
])
def test_hostile_or_unsupported_model_ids_are_refused(hostile):
    assert not valid_bedrock_model_id(hostile)


def test_over_long_ids_are_refused_at_the_edge_not_dropped_at_the_receipt():
    """129 characters is not an aesthetic limit.

    api/server.py's UsageReportRequest.model is max_length=128. A longer value
    raises ValidationError inside _hosted_proxy_receipt, which catches, logs
    once, and DROPS the billable row. Refusing here turns a silent revenue loss
    into a 400 the caller can see.
    """
    from api.server import UsageReportRequest

    assert UsageReportRequest.model_fields["model"].metadata[0].max_length == 128
    assert valid_bedrock_model_id("a" * 128)
    assert not valid_bedrock_model_id("a" * 129)


# ==========================================================================
# 2. The region as a hostname segment.
# ==========================================================================

@pytest.mark.parametrize("region", [
    "us-east-1", "us-west-2", "eu-central-1", "ap-southeast-2", "us-gov-west-1",
])
def test_real_regions_are_accepted(region):
    assert normalize_bedrock_region(region) == region


@pytest.mark.parametrize("hostile", [
    "",
    "us-east-1.evil.test",        # a dot would end the intended hostname
    "us-east-1/../../",
    "us-east-1:443",
    "us-east-1@evil.test",
    "us_east_1",
    "US-EAST-1x",
    "169.254.169.254",
    "us east 1",
    "us-east-1 evil",
])
def test_hostile_regions_are_refused(hostile):
    assert normalize_bedrock_region(hostile) == ""


def test_surrounding_whitespace_is_stripped_then_revalidated():
    """Tolerating a stray newline from a config file is safe; the value is still
    matched against the strict pattern AFTER stripping, so nothing whitespace-
    delimited can survive into a hostname."""
    assert normalize_bedrock_region("  us-east-1\n") == "us-east-1"


@pytest.mark.parametrize("value", [
    "us-east-1", "  US-East-1  ", "evil.test", "us-east-1/x", "", "\n\n",
    "us-east-1#@evil", "a" * 500,
])
def test_a_normalized_region_is_never_anything_but_a_region(value):
    """The invariant the hostname actually depends on: whatever comes back is
    either empty or contains only characters that cannot end a host label."""
    import re

    normalized = normalize_bedrock_region(value)
    assert normalized == "" or re.fullmatch(r"[a-z0-9-]+", normalized)


# ==========================================================================
# 3. The endpoint. This function decides where a bearer token goes.
# ==========================================================================

def test_endpoint_is_the_documented_bedrock_runtime_url():
    endpoint = bedrock_runtime_endpoint("us-east-1", MODEL, "invoke")
    assert endpoint == (
        "https://bedrock-runtime.us-east-1.amazonaws.com"
        "/model/us.anthropic.claude-opus-5-v1%3A0/invoke"
    ), "the colon must be percent-encoded exactly the way botocore encodes it"


def test_endpoint_host_is_always_bedrock_runtime_on_amazonaws():
    from urllib.parse import urlsplit

    for region in ("us-east-1", "eu-central-1", "ap-northeast-3", "us-gov-west-1"):
        host = urlsplit(bedrock_runtime_endpoint(region, MODEL, "invoke")).netloc
        assert host == f"bedrock-runtime.{region}.amazonaws.com"


@pytest.mark.parametrize("region,model_id,operation", [
    ("evil.test", MODEL, "invoke"),
    ("", MODEL, "invoke"),
    ("us-east-1", "a/b", "invoke"),
    ("us-east-1", "", "invoke"),
    ("us-east-1", MODEL, "converse"),          # not a lane this module serves
    ("us-east-1", MODEL, "../../../"),
])
def test_the_endpoint_builder_raises_rather_than_falling_back(region, model_id, operation):
    """A silent default here would send the caller's key somewhere unnamed."""
    with pytest.raises(ValueError):
        bedrock_runtime_endpoint(region, model_id, operation)


# ==========================================================================
# 4. Pricing: the region is part of the claim, not just the destination.
# ==========================================================================

def test_a_parity_region_prices_through_the_anthropic_table():
    alias = bedrock_priced_model(MODEL, "us-east-1")
    assert alias == "claude-opus-5"
    costs = calculate_costs(
        bedrock_provider_label(MODEL), alias, 1_000,
        TokenReceipt(fresh_input_tokens=1_000, output_tokens=100))
    assert costs["pricing_status"] == "priced"


@pytest.mark.parametrize("region", ["eu-central-1", "ap-southeast-2", "us-gov-west-1"])
def test_an_unverified_region_falls_to_the_unpriced_path(region):
    """Refusing to price costs Brevitas the fee and costs the customer nothing.

    Accepting `us.` inference profiles while pricing a request made in eu-* would
    contradict normalize_bedrock_model's refusal of the `eu.` profile, which
    exists because those rates sit ABOVE the ones in MODEL_PRICES.
    """
    assert bedrock_priced_model(MODEL, region) == ""
    costs = calculate_costs("bedrock", MODEL, 1_000,
                            TokenReceipt(fresh_input_tokens=1_000))
    assert costs["pricing_status"] == "unpriced"
    assert costs["measured_savings_usd"] is None


def test_the_raw_bedrock_id_is_never_accidentally_priced():
    """The whole reason normalize_bedrock_model exists: no key, and no prefix
    fallback, may ever match a raw Bedrock id."""
    for raw in (MODEL, "anthropic.claude-opus-5-v1:0", "eu.anthropic.claude-opus-5-v1:0"):
        assert calculate_costs("bedrock", raw, 10, TokenReceipt(
            fresh_input_tokens=10))["pricing_status"] == "unpriced"


# ==========================================================================
# 5. The event-stream decoder.
# ==========================================================================

def _frame(payload: bytes, headers: bytes = b"") -> bytes:
    """One AWS event-stream message. CRCs are zeroed: the decoder does not read
    them, deliberately (see brevitas/bedrock.py)."""
    total = 12 + len(headers) + len(payload) + 4
    return (struct.pack(">III", total, len(headers), 0)
            + headers + payload + struct.pack(">I", 0))


def _chunk(event: dict, headers: bytes = b"") -> bytes:
    envelope = {"bytes": base64.b64encode(json.dumps(event).encode()).decode()}
    return _frame(json.dumps(envelope).encode(), headers)


MESSAGE_START = {"type": "message_start", "message": {
    "id": "msg_bedrock_1",
    "usage": {"input_tokens": 100, "cache_read_input_tokens": 900,
              "cache_creation_input_tokens": 0, "output_tokens": 1}}}
MESSAGE_DELTA = {"type": "message_delta", "usage": {"output_tokens": 42}}


def test_decoder_recovers_the_anthropic_events():
    decoder = BedrockEventStreamDecoder()
    events = decoder.feed(_chunk(MESSAGE_START) + _chunk(MESSAGE_DELTA))
    assert [json.loads(event)["type"] for event in events] == [
        "message_start", "message_delta"]


def test_decoder_reassembles_frames_split_across_network_chunks():
    """Frame boundaries have nothing to do with TCP chunk boundaries."""
    stream = _chunk(MESSAGE_START) + _chunk(MESSAGE_DELTA)
    decoder = BedrockEventStreamDecoder()
    collected = []
    for index in range(0, len(stream), 3):
        collected.extend(decoder.feed(stream[index:index + 3]))
    assert len(collected) == 2


def test_decoder_skips_frames_that_are_not_chunk_envelopes():
    """Exception frames carry no usage; guessing at them writes a wrong receipt."""
    decoder = BedrockEventStreamDecoder()
    exception_frame = _frame(json.dumps({"message": "throttled"}).encode())
    events = decoder.feed(exception_frame + _chunk(MESSAGE_START))
    assert len(events) == 1
    assert json.loads(events[0])["type"] == "message_start"


@pytest.mark.parametrize("hostile", [
    struct.pack(">III", 0xFFFFFFFF, 0, 0),        # declares a 4GiB message
    struct.pack(">III", 32, 64, 0) + b"x" * 64,   # headers longer than the frame
    struct.pack(">III", 4, 0, 0),                 # shorter than its own overhead
    b"\x00" * 3,                                  # never even a full prelude
    b"not an event stream at all, just bytes",
])
def test_a_malformed_stream_never_raises_and_never_buffers(hostile):
    """The decoder runs inside the generator yielding the customer's response.

    Raising here would truncate a paid, in-flight completion in order to protect
    a metering read. Losing the receipt is the correct trade; losing the answer
    is not.
    """
    decoder = BedrockEventStreamDecoder()
    assert decoder.feed(hostile) == []
    # Whatever it decided, it keeps accepting bytes without raising and without
    # accumulating them: a stream that never yields a parseable frame must not
    # become a memory leak for the life of the response.
    for _ in range(200):
        assert decoder.feed(b"more bytes that are still not a frame") == []
    assert len(decoder._buffer) < 4096


def test_an_oversized_declared_length_latches_broken_instead_of_growing():
    decoder = BedrockEventStreamDecoder(max_message_bytes=1024)
    decoder.feed(struct.pack(">III", 4096, 0, 0))
    assert decoder.broken is True
    assert decoder.feed(b"x" * 100_000) == []
    assert len(decoder._buffer) == 0


def test_the_decoder_feeds_a_usable_receipt_into_the_sse_parser():
    """The end-to-end claim: binary frames in, a priced receipt out."""
    from brevitas.receipts import SSEUsageParser

    decoder = BedrockEventStreamDecoder()
    parser = SSEUsageParser("anthropic")
    for event in decoder.feed(_chunk(MESSAGE_START) + _chunk(MESSAGE_DELTA)):
        parser.feed(event + b"\n")
    receipt = parser.finish()
    assert receipt.fresh_input_tokens == 100
    assert receipt.cached_input_tokens == 900
    assert receipt.output_tokens == 42
    assert parser.response_id == "msg_bedrock_1"


# ==========================================================================
# 6. Handler behaviour.
# ==========================================================================

def _request(headers: dict[str, str], body: bytes = b"", path: str = INVOKE_PATH) -> Request:
    payload = body if body else json.dumps(BODY).encode()
    scope = {
        "type": "http", "method": "POST", "path": path, "raw_path": path.encode(),
        "headers": [(name.lower().encode(), value.encode())
                    for name, value in headers.items()],
        "client": ("127.0.0.1", 1), "query_string": b"", "server": ("test", 80),
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
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload

    async def aread(self):
        return self.content

    async def aclose(self):
        return None


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
        return captured.get("response") or _Upstream({"stop_reason": "end_turn"})

    async def reporter(key, payload):
        captured["receipts"].append(payload)

    monkeypatch.setattr(proxy, "_provider_request", fake_provider_request)
    monkeypatch.setattr(proxy, "_usage_reporter", reporter)
    monkeypatch.delenv("BREVITAS_BEDROCK_REGION", raising=False)
    return captured


def _invoke(request, model_id=MODEL, stream=False):
    return asyncio.run(proxy._proxy_bedrock(request, model_id, stream=stream))


# -- refusals ---------------------------------------------------------------

def test_a_request_with_no_credential_is_refused_before_the_body_is_read(lane):
    with pytest.raises(HTTPException) as caught:
        _invoke(_request({}))
    assert caught.value.status_code == 401
    assert "Bearer" in caught.value.detail
    assert "endpoint" not in lane, "no upstream may be contacted without a key"


def test_a_sigv4_signed_request_is_refused_with_the_reason(lane):
    """A SigV4 signature covers bytes a proxy cannot preserve.

    Forwarding it would produce an opaque upstream 403 and a receipt-less,
    invisible failure. The refusal names the supported alternative.
    """
    signed = ("AWS4-HMAC-SHA256 Credential=AKIA.../20260731/us-east-1/bedrock/"
              "aws4_request, SignedHeaders=host;x-amz-date, Signature=deadbeef")
    with pytest.raises(HTTPException) as caught:
        _invoke(_request({"authorization": signed}))
    assert caught.value.status_code == 400
    assert "Bedrock API key" in caught.value.detail
    assert "endpoint" not in lane


@pytest.mark.parametrize("model_id", [
    "a/b",
    "",
    "a" * 129,
    "a b",
    # A real inference-profile ARN. The "/" is what refuses it, and refusing is
    # free: normalize_bedrock_model returns "" for an ARN anyway, so such a
    # request could never have been priced.
    "arn:aws:bedrock:us-east-1:1:inference-profile/us.anthropic.claude-opus-5-v1:0",
])
def test_an_unsafe_model_id_never_reaches_a_url(lane, model_id):
    with pytest.raises(HTTPException) as caught:
        _invoke(_request({"authorization": BEARER}), model_id=model_id)
    assert caught.value.status_code == 400
    assert "endpoint" not in lane


@pytest.mark.parametrize("region", ["evil.test", "us-east-1.evil.test", "../.."])
def test_an_unsafe_region_never_reaches_a_hostname(lane, region):
    with pytest.raises(HTTPException) as caught:
        _invoke(_request({"authorization": BEARER,
                          proxy.BEDROCK_REGION_HEADER: region}))
    assert caught.value.status_code == 400
    assert "endpoint" not in lane


def test_converse_is_refused_with_a_reason_not_a_404(lane):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(proxy.proxy_bedrock_converse(
            _request({"authorization": BEARER}), MODEL))
    assert caught.value.status_code == 400
    assert "Converse" in caught.value.detail
    assert "InvokeModel" in caught.value.detail


@pytest.mark.parametrize("body", [
    # A Converse body posted to /invoke: no anthropic_version, blocks the token
    # counter does not read, usage fields the receipt parser drops.
    {"messages": [{"role": "user", "content": [{"text": "hi"}]}],
     "inferenceConfig": {"maxTokens": 64}},
    {"anthropic_version": "bedrock-2023-05-31", "messages": []},
    {"anthropic_version": "", "messages": [{"role": "user", "content": "hi"}]},
    {"inputText": "amazon titan style body"},
])
def test_a_non_anthropic_native_body_is_refused_rather_than_measured_as_zero(lane, body):
    with pytest.raises(HTTPException) as caught:
        _invoke(_request({"authorization": BEARER}, body=json.dumps(body).encode()))
    assert caught.value.status_code == 400
    assert "endpoint" not in lane, (
        "forwarding an unparseable schema would charge the customer at AWS while "
        "Brevitas recorded zero measured savings -- the exact silent-zero failure "
        "this lane refuses"
    )


# -- the happy path ---------------------------------------------------------

def test_the_request_reaches_the_documented_bedrock_url(lane):
    _invoke(_request({"authorization": BEARER}))
    assert lane["endpoint"] == (
        "https://bedrock-runtime.us-east-1.amazonaws.com"
        "/model/us.anthropic.claude-opus-5-v1%3A0/invoke")
    assert lane["method"] == "POST"
    # Its own circuit and its own metrics bucket, not Anthropic direct's.
    assert lane["provider"] == "bedrock"


def test_the_region_header_selects_the_region(lane):
    _invoke(_request({"authorization": BEARER,
                      proxy.BEDROCK_REGION_HEADER: "eu-central-1"}))
    assert lane["endpoint"].startswith(
        "https://bedrock-runtime.eu-central-1.amazonaws.com/")


def test_the_bearer_is_forwarded_and_nothing_else_is(lane):
    _invoke(_request({
        "authorization": BEARER,
        "content-type": "application/json",
        "x-amzn-bedrock-trace": "ENABLED",
        "x-brevitas-key": "bvt_our_own_tenant_secret",
        "cookie": "session=abc",
        "x-amz-date": "20260731T000000Z",
        "x-amz-content-sha256": "deadbeef",
        "host": "api.brevitassystems.com",
    }))
    forwarded = {name.lower(): value for name, value in lane["headers"].items()}
    assert forwarded["authorization"] == BEARER
    assert forwarded["x-amzn-bedrock-trace"] == "ENABLED"
    for leaked in ("x-brevitas-key", "cookie", "x-amz-date",
                   "x-amz-content-sha256", "host"):
        assert leaked not in forwarded, (
            f"{leaked} reached AWS. Our own tenant credential must never leave "
            "the building, and stale x-amz-* material makes Bedrock try to "
            "verify a signature over bytes we did not sign"
        )


def test_content_type_is_sent_exactly_once(lane):
    """A dict is not case-insensitive.

    Seeding {"Content-Type": ...} and then copying the caller's "content-type"
    over it leaves BOTH keys, and httpx puts two Content-Type headers on the
    wire -- which AWS answers with an opaque 400 that looks like a body problem.
    """
    _invoke(_request({"authorization": BEARER, "content-type": "application/json"}))
    names = [name.lower() for name in lane["headers"]]
    assert names.count("content-type") == 1


def test_content_type_is_defaulted_when_the_caller_omits_it(lane):
    _invoke(_request({"authorization": BEARER}))
    forwarded = {name.lower(): value for name, value in lane["headers"].items()}
    assert forwarded["content-type"] == "application/json"


def test_the_body_is_forwarded_byte_for_byte(lane):
    """A passthrough lane must not re-serialize: unknown fields and key order
    are the caller's, and a round-trip is a silent way to drop one."""
    raw = b'{"anthropic_version":"bedrock-2023-05-31","messages":[{"role":"user","content":"hi"}],"an_unknown_future_field":{"z":1,"a":2}}'
    _invoke(_request({"authorization": BEARER}, body=raw))
    assert lane["content"] == raw
    assert lane["json_body"] is None


def test_no_aws_credential_is_ever_stored(lane, monkeypatch):
    """The lane's central claim. The bearer may appear in exactly one place:
    the outbound Authorization header."""
    stored: list = []
    monkeypatch.setattr(proxy, "_cache_store",
                        lambda *args, **kwargs: stored.append(args))
    _invoke(_request({"authorization": BEARER}))
    assert stored == []                                  # cache is off by default
    for receipt in lane["receipts"]:
        assert BEARER not in json.dumps(receipt, default=str)


# -- the receipt ------------------------------------------------------------

def _only_receipt(lane):
    assert len(lane["receipts"]) == 1, lane["receipts"]
    return lane["receipts"][0]


def test_the_receipt_bills_as_bedrock_at_the_normalized_alias(lane):
    lane["response"] = _Upstream({
        "id": "msg_1", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 900,
                  "cache_creation_input_tokens": 0, "output_tokens": 42},
    })
    _invoke(_request({"authorization": BEARER}))
    receipt = _only_receipt(lane)

    assert receipt["provider"] == "bedrock", (
        "recording 'anthropic' would erase the route and make a future "
        "('bedrock', model) price row unreachable")
    assert receipt["model"] == "claude-opus-5", (
        "the raw Bedrock id matches no MODEL_PRICES key, so recording it "
        "verbatim prices every row as unpriced and bills nothing")
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


def test_a_passthrough_row_reports_exactly_zero_transformation_delta(lane):
    """Honest by construction: round 1 injects nothing, so it claims nothing.

    compressed_tokens == baseline_tokens is the only truthful pair for a lane
    that forwards the caller's bytes unchanged. cache_attributable stays False so
    the customer's own cache markers never enter the billable number.
    """
    lane["response"] = _Upstream({"id": "msg_1", "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 10, "output_tokens": 5}})
    _invoke(_request({"authorization": BEARER}))
    receipt = _only_receipt(lane)
    assert receipt["compressed_tokens"] == receipt["baseline_tokens"]
    assert receipt["cache_attributable"] is False
    assert receipt["strategy"] == "passthrough"


def test_an_unpriced_region_still_records_a_row_it_just_earns_nothing(lane):
    lane["response"] = _Upstream({"id": "msg_1", "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 10, "output_tokens": 5}})
    _invoke(_request({"authorization": BEARER,
                      proxy.BEDROCK_REGION_HEADER: "eu-central-1"}))
    receipt = _only_receipt(lane)
    # A SENTINEL, not the raw id. Falling back to the raw id was a real hole:
    # canonical_provider reads "claude…"/"gpt-…"/"deepseek…" straight off the
    # model string, so a namespace-free id that normalize_bedrock_model
    # deliberately REFUSES would still have priced at the originating family's
    # list rate -- the exact smuggling this lane's mapper exists to prevent.
    # The sentinel keeps the asked-for id visible while being unresolvable.
    assert receipt["model"] == f"unverified:{MODEL}"
    assert MODEL in receipt["model"], "the asked-for id must stay diagnosable"
    assert calculate_costs(receipt["provider"], receipt["model"], 10,
                           TokenReceipt(fresh_input_tokens=10))["pricing_status"] == "unpriced"


@pytest.mark.parametrize("smuggled", [
    "claude-opus-5",       # namespace-free: refused by normalize_bedrock_model
    "gpt-4o",
    "deepseek-chat",
])
def test_a_refused_model_id_cannot_be_smuggled_into_pricing(smuggled):
    """The regression test for the `or model_id` fallback.

    normalize_bedrock_model refuses these so a caller cannot name an arbitrary
    model in the Bedrock lane and have it priced. Handing the raw id to
    calculate_costs undid that one frame up.
    """
    from brevitas.bedrock import normalize_bedrock_model

    assert normalize_bedrock_model(smuggled) == "", "fixture assumes a refused id"
    raw = calculate_costs("bedrock", smuggled, 10, TokenReceipt(fresh_input_tokens=10))
    assert raw["pricing_status"] == "priced", (
        "fixture assumes the RAW id would have priced; if it no longer does, "
        "canonical_provider changed and this test should be re-derived"
    )
    guarded = calculate_costs("bedrock", f"unverified:{smuggled}", 10,
                              TokenReceipt(fresh_input_tokens=10))
    assert guarded["pricing_status"] == "unpriced"
    assert guarded["measured_savings_usd"] is None


def test_a_failed_upstream_writes_no_receipt(lane):
    lane["response"] = _Upstream({"message": "AccessDeniedException"}, status_code=403)
    response = _invoke(_request({"authorization": BEARER}))
    assert response.status_code == 403
    assert lane["receipts"] == []


def test_the_lane_never_originates_a_warming_ping(lane, monkeypatch):
    """Warming is a request Brevitas initiates, which needs a credential we do
    not hold. Observation must not even be recorded for this lane."""
    observed = []
    monkeypatch.setattr(proxy, "_observe_warm_prefix",
                        lambda *args, **kwargs: observed.append(args))
    lane["response"] = _Upstream({"id": "msg_1", "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 10, "output_tokens": 5}})
    _invoke(_request({"authorization": BEARER}))
    assert observed == []


# -- streaming --------------------------------------------------------------

def test_the_streamed_lane_measures_usage_out_of_the_binary_frames(lane, monkeypatch):
    """Without the frame decoder every streamed call would record zero tokens.

    A zero-token receipt is reported as `passthrough:missing_receipt`: the call
    succeeds, AWS charges for it, and Brevitas measures nothing.
    """
    stream_bytes = _chunk(MESSAGE_START) + _chunk(MESSAGE_DELTA)
    lane["response"] = _Upstream(
        content=stream_bytes, content_type="application/vnd.amazon.eventstream")

    class _Http:
        async def iter_bytes(self, provider, response):
            lane["stream_provider"] = provider
            for index in range(0, len(stream_bytes), 7):
                yield stream_bytes[index:index + 7]

    monkeypatch.setattr(proxy, "provider_http", _Http())
    response = _invoke(_request({"authorization": BEARER}, path=STREAM_PATH), stream=True)

    async def drain():
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(drain()) == stream_bytes, "response bytes must be verbatim"
    assert lane["stream"] is True
    assert lane["stream_provider"] == "bedrock"
    assert lane["endpoint"].endswith("/invoke-with-response-stream")

    receipt = _only_receipt(lane)
    assert receipt["provider"] == "bedrock"
    assert receipt["model"] == "claude-opus-5"
    assert receipt["fresh_input_tokens"] == 100
    assert receipt["cached_input_tokens"] == 900
    assert receipt["output_tokens"] == 42
    assert receipt["receipt_available"] is True


def test_a_corrupt_stream_still_delivers_every_byte_to_the_caller(lane, monkeypatch):
    """Metering degrades; the customer's answer does not."""
    stream_bytes = b"\xff\xff\xff\xff garbage that is not an event stream"
    lane["response"] = _Upstream(
        content=stream_bytes, content_type="application/vnd.amazon.eventstream")

    class _Http:
        async def iter_bytes(self, provider, response):
            yield stream_bytes

    monkeypatch.setattr(proxy, "provider_http", _Http())
    response = _invoke(_request({"authorization": BEARER}, path=STREAM_PATH), stream=True)

    async def drain():
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(drain()) == stream_bytes
    receipt = _only_receipt(lane)
    # Loud rather than silent: a missing receipt is labelled, not zero-filled.
    assert receipt["receipt_available"] is False
    assert receipt["strategy"].endswith(":missing_receipt")


# ==========================================================================
# 7. The response cache: the one place this lane earns real savings today.
# ==========================================================================
#
# A passthrough lane transforms nothing, so its transformation delta is honestly
# zero. A semantic-cache hit is different: it touches no upstream at all, so it
# is 100% savings on a byte-preserving strategy -- billable on this route exactly
# as on every other. That makes the cache plumbing revenue-critical here.

COMPLETE_RESPONSE = {
    "id": "msg_1", "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 100, "output_tokens": 42},
}


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


def _with_cache(lane, monkeypatch, cache):
    monkeypatch.setattr(proxy, "_get_cache", lambda: cache)
    request = _request({"authorization": BEARER})
    request.state.brevitas_cache_enabled = True
    return request


def test_a_cache_hit_replays_without_touching_aws_and_bills_as_bedrock(lane, monkeypatch):
    cache = _Cache(hit=_Hit())
    response = _invoke(_with_cache(lane, monkeypatch, cache))

    assert response.status_code == 200
    assert "endpoint" not in lane, "a hit must not reach an upstream"
    receipt = _only_receipt(lane)
    assert receipt["provider"] == "bedrock"
    assert receipt["model"] == "claude-opus-5"
    assert receipt["strategy"] == "exact_cache"
    assert receipt["cache_attributable"] is True
    # The forward link to the paid ancestor: without it the zero-spend row is
    # indistinguishable from a forged one and bills nothing.
    assert receipt["savings_anchor_request_id"] == "proxy:the_call_that_paid"


def test_a_complete_bedrock_response_is_actually_cacheable(lane, monkeypatch):
    """The regression test for the wire-shape split.

    Bedrock's InvokeModel response is an Anthropic Messages response, but its
    billing label is "bedrock". Anything that reads the response SHAPE has to
    follow the wire, not the label -- read through the OpenAI branch, this
    response has no `choices`, so _response_complete would call every Bedrock
    answer truncated and the cache would silently never fill. A cache that never
    fills never hits, and a hit is where this lane's savings come from.
    """
    cache = _Cache()
    lane["response"] = _Upstream(COMPLETE_RESPONSE)
    _invoke(_with_cache(lane, monkeypatch, cache))

    assert len(cache.stored) == 1, "a complete response was not stored"
    entry = cache.stored[0]
    assert (entry["prompt_tokens"], entry["completion_tokens"]) == (100, 42)


def test_a_truncated_bedrock_response_is_not_cached(lane, monkeypatch):
    cache = _Cache()
    lane["response"] = _Upstream({**COMPLETE_RESPONSE, "stop_reason": "max_tokens"})
    _invoke(_with_cache(lane, monkeypatch, cache))
    assert cache.stored == []


def test_the_cache_namespace_pins_both_the_raw_id_and_the_region(lane, monkeypatch):
    """A Bedrock answer must never replay for an Anthropic-direct call, for a
    different Bedrock model id, or across regions."""
    cache = _Cache()
    lane["response"] = _Upstream(COMPLETE_RESPONSE)
    _invoke(_with_cache(lane, monkeypatch, cache))
    assert cache.looked_up == ("bedrock", f"{MODEL}@us-east-1")
    assert cache.stored[0]["model"] == f"{MODEL}@us-east-1"


def test_the_cached_body_carries_no_aws_credential(lane, monkeypatch):
    """_cache_body varies on a salted digest of the credential, never the value."""
    cache = _Cache()
    lane["response"] = _Upstream(COMPLETE_RESPONSE)
    _invoke(_with_cache(lane, monkeypatch, cache))
    serialized = json.dumps(cache.stored[0]["body"], default=str)
    assert BEARER not in serialized
    assert "bedrock-api-key-value" not in serialized


def test_a_streamed_call_never_consults_the_cache(lane, monkeypatch):
    """cacheable() already refuses streams; asserting it here keeps the lane from
    returning a non-stream JSON body to a client awaiting event-stream frames."""
    cache = _Cache(hit=_Hit())
    stream_bytes = _chunk(MESSAGE_START)
    lane["response"] = _Upstream(
        content=stream_bytes, content_type="application/vnd.amazon.eventstream")

    class _Http:
        async def iter_bytes(self, provider, response):
            yield stream_bytes

    monkeypatch.setattr(proxy, "provider_http", _Http())
    request = _with_cache(lane, monkeypatch, cache)
    _invoke(request, stream=True)
    assert cache.looked_up is None


# ==========================================================================
# 8. Routing: the handler's model_id can never contain a separator.
# ==========================================================================

def _bedrock_routes():
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
            if getattr(route, "path", "").startswith("/bedrock/")]


def _matches(path):
    scope = {"type": "http", "method": "POST", "path": path, "headers": [],
             "root_path": ""}
    for route in _bedrock_routes():
        match, child = route.matches(scope)
        if match == Match.FULL:
            return child["path_params"]["model_id"]
    return None


def test_the_lane_registers_exactly_the_four_bedrock_routes():
    assert sorted(route.path for route in _bedrock_routes()) == [
        "/bedrock/model/{model_id}/converse",
        "/bedrock/model/{model_id}/converse-stream",
        "/bedrock/model/{model_id}/invoke",
        "/bedrock/model/{model_id}/invoke-with-response-stream",
    ]


def test_the_model_id_parameter_cannot_span_a_path_separator():
    """`{model_id}` is `[^/]+`, not `:path`.

    A greedy converter would hand the handler "a/b" and make the URL builder the
    only thing standing between a crafted path and a re-targeted upstream. The
    router refusing to match it is a second, independent stop.
    """
    assert _matches(INVOKE_PATH) == MODEL
    assert _matches("/bedrock/model/a/b/invoke") is None
    assert _matches("/bedrock/model//invoke") is None
    assert _matches("/bedrock/model/x/invoke/extra") is None


def test_the_two_invoke_shapes_do_not_shadow_each_other():
    assert _matches(STREAM_PATH) == MODEL
    assert _matches(f"/bedrock/model/{MODEL}/invoke") == MODEL


def test_normalize_and_label_still_agree_with_the_lane():
    """Pins the two functions the lane depends on against its own constants."""
    assert normalize_bedrock_model(MODEL) == "claude-opus-5"
    assert bedrock_provider_label(MODEL) == "bedrock"
