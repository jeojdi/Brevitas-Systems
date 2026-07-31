"""AWS Bedrock model identifiers, and how they map onto priced models.

WHY THIS MODULE EXISTS. Bedrock names a model differently from every other route
Brevitas proxies. Anthropic direct sends ``claude-opus-5``; Bedrock sends
``us.anthropic.claude-opus-5-v1:0`` -- a cross-region inference-profile prefix, a
vendor namespace, and a version suffix wrapped around the same model.

Nothing in the pricing path can see through that. ``MODEL_PRICES`` is keyed on
the bare alias, and ``model_price``'s snapshot fallback only matches
``startswith(f"{known_model}-")``, so ``anthropic.claude-opus-5-v1:0`` matches
nothing at all. An unmapped id therefore resolves to ``pricing_status="unpriced"``
with ``measured_savings_usd=None``, which the settlement evidence function
excludes entirely -- so it fails CLOSED to $0 rather than mispricing. That is the
right failure, but it is not a useful product, which is what this map is for.

WHY AN EXPLICIT MAP RATHER THAN A REGEX-AND-HOPE. Getting this wrong does not
raise; it silently prices one model as another, and the fee is a percentage of a
number derived from that price. So the rule is: recognise exactly what we have
verified, and refuse everything else into the unpriced path.

REGIONAL PREFIXES ARE NOT COSMETIC. ``us.`` and ``global.`` inference profiles
bill at the same on-demand rate as the bare model, so they normalise. ``eu.`` and
``apac.`` cross-region profiles are priced ABOVE that (roughly 10% at the time of
writing), so normalising them would understate what the customer actually paid --
which INFLATES apparent savings and therefore the invoice. They are refused
instead, and refusal costs us revenue rather than costing the customer money.
That asymmetry is deliberate and should survive any future edit here.

WHAT ELSE LIVES HERE. The lane's two other Bedrock-only facts, kept next to the
pricing rule they interact with: how a caller-supplied model id and region become
a ``bedrock-runtime`` URL without becoming an SSRF, and how to read a usage
receipt out of ``application/vnd.amazon.eventstream`` (Bedrock streams binary
frames, not SSE, so the receipt is not on a ``data:`` line). Both are pure
functions with no I/O and no credentials -- the lane forwards the caller's bearer
token and never holds one.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import struct
from urllib.parse import quote

#: Inference-profile prefixes that bill at the bare model's on-demand rate.
#: Verified against AWS's published Bedrock pricing for Claude models.
_PARITY_REGION_PREFIXES = ("us.", "global.")

#: Cross-region profiles that bill ABOVE parity. Normalising these would
#: understate actual spend and overstate savings, so they are refused into the
#: unpriced path instead. See the module docstring.
_PREMIUM_REGION_PREFIXES = ("eu.", "apac.", "us-gov.")

#: Vendor namespaces Bedrock puts in front of the model name.
_VENDOR_NAMESPACES = ("anthropic.", "meta.", "mistral.", "amazon.", "cohere.", "ai21.")

#: Bedrock's trailing version marker: ``-v1:0``, ``-v2:1``, ``:0`` and friends.
_VERSION_SUFFIX = re.compile(r"(?:-v\d+)?:\d+$")


def normalize_bedrock_model(model_id: str) -> str:
    """Map a Bedrock model id onto the bare alias MODEL_PRICES is keyed on.

    Returns "" when the id is not one we have verified pricing parity for, which
    routes the request into the unpriced path: savings are not computed, the row
    is invisible to settlement evidence, and the customer is billed nothing for
    it. Refusing is always safe; guessing is not.

    >>> normalize_bedrock_model("us.anthropic.claude-opus-5-v1:0")
    'claude-opus-5'
    >>> normalize_bedrock_model("anthropic.claude-haiku-4-5-20251001-v1:0")
    'claude-haiku-4-5-20251001'
    >>> normalize_bedrock_model("eu.anthropic.claude-opus-5-v1:0")
    ''
    """
    raw = (model_id or "").strip().lower()
    if not raw:
        return ""

    # Premium cross-region profiles are checked BEFORE the parity prefixes,
    # because refusing must win over any later rule that would accept them.
    if raw.startswith(_PREMIUM_REGION_PREFIXES):
        return ""

    for prefix in _PARITY_REGION_PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break

    for namespace in _VENDOR_NAMESPACES:
        if raw.startswith(namespace):
            raw = raw[len(namespace):]
            break
    else:
        # No vendor namespace at all means this is not a Bedrock id. Refuse
        # rather than passing a bare name through, so a caller cannot smuggle an
        # arbitrary model into the Bedrock lane and have it priced.
        return ""

    bare = _VERSION_SUFFIX.sub("", raw).strip("-")
    return bare


def bedrock_provider_label(model_id: str) -> str:
    """The provider label a Bedrock receipt is recorded under.

    Always ``"bedrock"`` -- pricing follows the bytes, and the bytes went to
    Bedrock. ``model_price`` resolves the route first and falls back to the
    originating family, so a ("bedrock", model) row wins where one exists and the
    Anthropic table serves where it does not. Recording ``"anthropic"`` here
    would erase the distinction and make a future Bedrock price row unreachable
    again.
    """
    return "bedrock"


# ── The model id as a URL segment ─────────────────────────────────────────────
# The Bedrock lane is the only route that takes a caller-controlled string and
# interpolates it into an upstream URL. api/server.py's proxy gate refuses any
# path whose routed form differs from the bytes on the wire, so by the time a
# handler sees this value it is already free of encoded separators, traversal and
# control characters -- but the gate protects the ROUTER, not the URL we build.
# This is the second, independent check, and it is deliberately a positive
# charset rather than a blocklist.
#
# 128 is not cosmetic: it is api/server.py's UsageReportRequest.model max_length.
# A longer id raises ValidationError inside _hosted_proxy_receipt, which is
# caught, logged once, and DROPS the billable row -- so an over-long id must be
# refused loudly at the edge instead of silently unbilled at the end.
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: AWS region ids: ``us-east-1``, ``ap-southeast-2``, ``us-gov-west-1``. No dots,
#: no slashes, no uppercase, no percent escapes -- so the value cannot leave the
#: ``bedrock-runtime.{region}.amazonaws.com`` host it is interpolated into.
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+){1,2}-\d{1,2}$")

#: Regions whose Claude on-demand prices we have treated as equal to the rates in
#: receipts.MODEL_PRICES. This is the SAME claim ``_PARITY_REGION_PREFIXES``
#: makes, seen from the other side: the ``us.`` inference profile spans exactly
#: the US commercial regions, so accepting ``us.`` while pricing a request made
#: in, say, ``eu-central-1`` would contradict the refusal of ``eu.`` two
#: constants above. Every other region routes fine and prices as UNPRICED, which
#: bills the customer nothing. Widening this set is a pricing claim and needs the
#: regional rate card as evidence, not a guess.
_PARITY_REGIONS = frozenset({"us-east-1", "us-east-2", "us-west-1", "us-west-2"})

#: The runtime operations this lane serves. Converse is deliberately absent --
#: see brevitas/proxy.py's refusal for why measuring it would report zero.
BEDROCK_INVOKE = "invoke"
BEDROCK_INVOKE_STREAM = "invoke-with-response-stream"
_OPERATIONS = frozenset({BEDROCK_INVOKE, BEDROCK_INVOKE_STREAM})


def valid_bedrock_model_id(model_id: str) -> bool:
    """True when the id is safe to place in an upstream URL and on a receipt."""
    return bool(_MODEL_ID.match(model_id or ""))


def normalize_bedrock_region(region: str) -> str:
    """The lower-cased region, or "" when it is not a well-formed AWS region id."""
    value = (region or "").strip().lower()
    return value if _REGION.match(value) else ""


def bedrock_runtime_endpoint(region: str, model_id: str, operation: str) -> str:
    """Build the bedrock-runtime URL, or raise ValueError rather than guess.

    Raising is the point. Every input is caller-influenced, so a silent fallback
    (a default region, a stripped model id) would send the caller's bearer token
    somewhere they did not name. The three checks below are re-run here even
    though the handler already ran them, because this function is what actually
    decides the destination host.

    The model id is percent-encoded exactly the way botocore encodes a non-greedy
    URI label (``SAFE_CHARS = '-._~'``), so ``:`` becomes ``%3A`` -- the same
    bytes a real ``bedrock-runtime`` SDK call puts on the wire.
    """
    normalized_region = normalize_bedrock_region(region)
    if not normalized_region:
        raise ValueError("bedrock region is not a well-formed AWS region id")
    if not valid_bedrock_model_id(model_id):
        raise ValueError("bedrock model id is not a safe URL segment")
    if operation not in _OPERATIONS:
        raise ValueError("unsupported bedrock runtime operation")
    return (f"https://bedrock-runtime.{normalized_region}.amazonaws.com"
            f"/model/{quote(model_id, safe='-._~')}/{operation}")


def bedrock_priced_model(model_id: str, region: str) -> str:
    """The priced alias for this id IN THIS REGION, or "" to leave it unpriced.

    Region is part of the pricing claim, not just of the destination: see
    ``_PARITY_REGIONS``. Returning "" costs Brevitas the fee on that row and
    costs the customer nothing, which is the direction this module always errs.
    """
    if normalize_bedrock_region(region) not in _PARITY_REGIONS:
        return ""
    return normalize_bedrock_model(model_id)


# ── InvokeModelWithResponseStream framing ─────────────────────────────────────
# Bedrock streams `application/vnd.amazon.eventstream`, not SSE, so the usage
# receipt is inside a binary frame rather than on a `data:` line. Without this
# decoder every streamed Bedrock call would finish with total_tokens == 0, which
# _record_receipt reports as `strategy:missing_receipt` -- the request succeeds,
# the customer is charged by AWS, and Brevitas measures nothing. Silent zero
# measurement is the exact failure this build refuses elsewhere (Converse), so
# the stream is decoded rather than shipped blind.
#
# Frame layout (AWS event stream):
#     [total_length u32][headers_length u32][prelude_crc u32]
#     [headers ..headers_length][payload ..][message_crc u32]
# A `chunk` event's payload is `{"bytes": "<base64>", ...}` whose decoded value
# is one Anthropic streaming event (message_start / message_delta / ...), which
# receipts.SSEUsageParser already understands.
#
# DELIBERATELY NOT CRC-VERIFIED. The bytes are forwarded to the caller verbatim
# whatever this decoder concludes, so the only thing a CRC check could change is
# whether Brevitas reads a receipt it could otherwise read. A wrong CRC
# implementation would therefore silently unbill every streamed call -- strictly
# worse than not checking. Framing bounds are enforced instead, because those
# protect memory.
_PRELUDE_BYTES = 12
_FRAME_OVERHEAD_BYTES = 16          # 12-byte prelude + trailing 4-byte message CRC
#: AWS's documented maximum event-stream message size. A declared length above it
#: is a framing error, not a big message.
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def _decode_chunk_payload(payload: bytes) -> bytes | None:
    """The Anthropic event inside one `chunk` frame, or None for anything else.

    Exception frames (`modelStreamErrorException` and friends) and any payload
    that is not a base64 `bytes` envelope return None: they carry no usage, and
    guessing at an unrecognised shape is how a wrong receipt gets written.
    """
    try:
        event = json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    encoded = event.get("bytes")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


class BedrockEventStreamDecoder:
    """Incrementally pull Anthropic events out of a vnd.amazon.eventstream body.

    NEVER RAISES, by contract. It runs inside the generator that yields the
    customer's response bytes, so an exception here would truncate a paid,
    in-flight completion in order to protect a metering read. On any framing
    fault it latches ``broken`` and returns nothing further; the caller keeps
    forwarding bytes and simply loses the receipt for that request.
    """

    def __init__(self, max_message_bytes: int = _MAX_MESSAGE_BYTES) -> None:
        self._buffer = bytearray()
        self._max_message_bytes = max(_FRAME_OVERHEAD_BYTES, int(max_message_bytes))
        self.broken = False

    def feed(self, chunk: bytes) -> list[bytes]:
        """Return the Anthropic events completed by this chunk (possibly none)."""
        if self.broken:
            return []
        try:
            return self._feed(chunk)
        except Exception:
            self._fail()
            return []

    def _fail(self) -> None:
        self.broken = True
        self._buffer.clear()

    def _feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        events: list[bytes] = []
        while True:
            if len(self._buffer) < _PRELUDE_BYTES:
                return events
            total_length, headers_length = struct.unpack_from(">II", self._buffer, 0)
            # Bound BEFORE trusting the length: a hostile or corrupt prelude is
            # the one way this decoder could be made to buffer without limit.
            if (total_length > self._max_message_bytes
                    or headers_length > total_length
                    or total_length < _FRAME_OVERHEAD_BYTES + headers_length):
                self._fail()
                return events
            if len(self._buffer) < total_length:
                return events               # frame still arriving
            payload_start = _PRELUDE_BYTES + headers_length
            payload = bytes(self._buffer[payload_start:total_length - 4])
            del self._buffer[:total_length]
            event = _decode_chunk_payload(payload)
            if event is not None:
                events.append(event)
