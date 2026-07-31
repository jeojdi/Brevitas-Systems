"""Azure OpenAI: a deployment name in the path, and a model only the response knows.

WHY THIS IS NOT JUST THE OPENAI LANE WITH A DIFFERENT HOST. Azure's data plane is
OpenAI-shaped on the wire and nothing like it in identity:

  * THE REQUEST NAMES A DEPLOYMENT, NEVER A MODEL. openai-python's AzureOpenAI
    builds ``{endpoint}/openai/deployments/{deployment}/chat/completions`` and
    pastes ``body["model"]`` into that path, so on Azure ``body["model"]`` IS the
    deployment alias -- every Azure sample says so in as many words ("Replace
    with your model deployment name"). A deployment is customer-named:
    ``gpt-4.1`` and ``prod-cheap-1`` are equally legal names, for the same model
    or for different ones. Pricing off it is pricing off a label the customer
    chose, so this module never does.

  * THE MODEL ARRIVES IN THE RESPONSE. Azure Chat Completions returns the dated
    snapshot in ``body.model`` (``gpt-4.1-2025-04-14``). That is the only
    priceable identifier anywhere in the exchange, and it exists only AFTER the
    upstream call -- which is why this lane cannot make a routing or optimisation
    decision on the model and does not pretend to. It is also why the value is
    treated as high-confidence but NOT contractual: Azure's own REST reference
    describes ``model`` generically and warns that response objects may change,
    so an absent or unrecognised value falls to unpriced rather than to a guess.

  * ``api-version`` IS MANDATORY AND LIVES IN THE QUERY STRING. It is the only
    query parameter the deployments data plane takes, and a request without it
    does not reach a deployment at all.

  * THE SKU IS UNRECOVERABLE. Global Standard, DataZone Standard, regional
    Standard and Provisioned/PTU are the same model at different per-token
    prices, and the deployment type appears in no response field and no
    documented response header. It cannot be read; it can only be DECLARED.

THE PRICING ASYMMETRY, which must survive any future edit here.
``brevitas/receipts.py``'s canonical_provider already folds the ``azure_openai``
route onto the first-party ``openai`` price table. That claim is defensible only
for the deployment type Azure sells at OpenAI's own per-token rates; every other
Azure deployment type costs MORE per token than that. So this module gates the
pre-existing claim rather than widening it:

  * a DECLARED parity SKU prices through the OpenAI table;
  * every other SKU, and an undeclared SKU, is UNPRICED and bills $0.

That is safe in exactly one direction. Under-declaring -- claiming the cheapest
SKU while actually running a dearer one -- scales both legs of
``baseline_cost - actual_cost`` by the same factor, so it can only UNDERSTATE
measured savings and therefore Brevitas's own fee. Over-declaring cannot inflate
anything, because a non-parity SKU does not price at all. And Provisioned/PTU
capacity is bought by the hour with no marginal per-token price, so a PTU
customer correctly bills $0 instead of being charged a percentage of a token
cost they never paid. Widening _PARITY_SKUS is a pricing claim and needs a rate
card as evidence, not a guess.

WHAT ELSE LIVES HERE. The lane's Azure-only facts, kept next to the pricing rule
they interact with: how a caller-supplied resource label becomes a hostname
without becoming an SSRF, which api-versions can emit usage on a stream at all,
and how to read the model out of the first SSE frame. Every function is pure,
does no I/O, and holds no credential -- the lane forwards the caller's own
``api-key``/``Authorization`` and never mints or stores an Azure credential.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

#: The one Azure OpenAI data-plane host shape this lane will build. Grounded in
#: this repo's own recorded fact about Azure endpoints -- brevitas/scanner/broad.py
#: matches Azure OpenAI credentials against ``[a-z0-9.-]*\.openai\.azure\.com``.
#: Foundry/Cognitive-Services aliases and the sovereign clouds (``.azure.us``,
#: ``.azure.cn``) are deliberately absent: they are separate price lists, and a
#: lane that silently routes to one while pricing from another is exactly the
#: mistake the SKU rule above exists to prevent. A caller on one of those gets a
#: 400 naming the supported shape, which is a loud loss rather than a quiet one.
AZURE_HOST_SUFFIX = "openai.azure.com"

#: Azure resource names are a DNS label: alphanumerics and hyphens, no dots (a
#: dot would end the hostname we intend and start one the caller chose), no
#: underscores, no percent escapes, no port, no userinfo. Bounded at 63 because
#: that is the maximum length of a DNS label, not because Azure says so.
_RESOURCE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

#: Azure deployment names: alphanumerics, ``-``, ``_`` and ``.``, up to 64. The
#: dot is NOT optional to support -- deployments named after their model
#: (``gpt-4.1``) are the single most common naming convention in Azure's own
#: docs, and refusing them would refuse most real traffic. Every character in
#: this class is URI-unreserved, so a real client never percent-encodes a
#: deployment name and the proxy gate's canonical-path check never has to.
_DEPLOYMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Dated api-versions, GA or preview. The value is re-minted into the upstream
#: query string from this match, so nothing else can ever appear there.
_API_VERSION = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(-preview)?$")

#: Model identifiers that are safe to put on a receipt. 128 is not cosmetic: it
#: is api/server.py's UsageReportRequest.model max_length, and a longer value
#: raises ValidationError inside _hosted_proxy_receipt, which catches, logs once,
#: and DROPS the billable row. An unreadable model must cost us the price, never
#: the whole receipt.
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: Prefix for the receipt model of a row this lane refuses to price. It has to be
#: something MODEL_PRICES can NEVER resolve, because the alternative -- reporting
#: the deployment name bare -- would price a deployment a customer happened to
#: name "gpt-4.1" at gpt-4.1 rates, which is precisely the guess this module
#: exists to avoid. ``:`` cannot begin a MODEL_PRICES key and the whole string
#: cannot prefix-match one, so the row resolves to "unpriced" by construction.
#: tests/test_azure_lane.py pins that against every row in the table.
AZURE_UNPRICED_PREFIX = "deployment:"

#: Azure deployment types, canonicalised. Keyed on the value with every
#: non-alphanumeric character removed, so the ARM SKU names (``GlobalStandard``),
#: the portal's prose ("Global Standard") and a hyphenated header value all land
#: on the same canonical string.
_SKU_ALIASES = {
    "globalstandard": "global-standard",
    "datazonestandard": "datazone-standard",
    "standard": "standard",
    "regionalstandard": "standard",
    "globalprovisioned": "global-provisioned",
    "globalprovisionedmanaged": "global-provisioned",
    "datazoneprovisioned": "datazone-provisioned",
    "datazoneprovisionedmanaged": "datazone-provisioned",
    "provisioned": "provisioned",
    "provisionedmanaged": "provisioned",
    "ptu": "provisioned",
    "globalbatch": "global-batch",
    "datazonebatch": "datazone-batch",
    "batch": "batch",
}

#: The deployment types whose per-token rates are claimed equal to the
#: first-party OpenAI rows in brevitas/receipts.py. Exactly one, and widening it
#: is a pricing claim -- see the module docstring.
_PARITY_SKUS = frozenset({"global-standard"})

#: First Azure api-version able to emit a usage object inside a stream: Azure
#: added ``stream_options``/``include_usage`` in 2024-09-01-preview. On anything
#: older the parameter is not merely useless, it is an unrecognised request
#: argument -- so asking for it would 400 live customer traffic. Compared as a
#: date tuple rather than lexically so a future four-digit year cannot silently
#: sort wrong.
_STREAM_USAGE_SINCE = (2024, 9, 1)

AZURE_PROVIDER = "azure_openai"


def azure_provider_label() -> str:
    """The billing label for this lane.

    A function, not a bare constant, for the same reason bedrock_provider_label
    is one: the label is what pricing keys on, so it should be reached through a
    named thing that a grep for "who decides the billed provider" finds.
    """
    return AZURE_PROVIDER


def normalize_azure_resource(resource: str) -> str:
    """The lower-cased resource label, or "" when it is not a safe DNS label.

    This is the only caller-controlled value that becomes part of a hostname, and
    the caller's own Azure credential is forwarded to whatever that hostname
    resolves to. "" means refuse; there is deliberately no default and no repair.
    """
    value = (resource or "").strip().lower()
    return value if _RESOURCE.match(value) else ""


def valid_azure_deployment(deployment: str) -> bool:
    """True when the deployment name is safe as a URL segment and on a receipt."""
    return bool(_DEPLOYMENT.match(deployment or ""))


def normalize_azure_api_version(api_version: str) -> str:
    """The api-version, or "" when it is not a well-formed dated version.

    Range-checked, not just shape-checked: ``2024-99-99`` matches the digits but
    is not a date, and this value is compared against _STREAM_USAGE_SINCE to
    decide whether asking Azure for stream usage is safe.
    """
    value = (api_version or "").strip().lower()
    match = _API_VERSION.match(value)
    if not match:
        return ""
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not (2000 <= year <= 2999 and 1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return value


def azure_stream_usage_supported(api_version: str) -> bool:
    """True when this api-version can return a usage object on a stream.

    Fails CLOSED for an unparseable version: no injection, no usage, an honest
    missing_receipt. The other direction would send ``stream_options`` to an
    api-version that rejects unknown request arguments and turn a metering
    nicety into a 400 on a paying customer's request.
    """
    match = _API_VERSION.match(normalize_azure_api_version(api_version))
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2)),
            int(match.group(3))) >= _STREAM_USAGE_SINCE


def azure_chat_endpoint(resource: str, deployment: str, api_version: str) -> str:
    """Build the Azure Chat Completions URL, or raise ValueError rather than guess.

    Raising is the point. Both path inputs are caller-influenced and the caller's
    own Azure key rides along, so a silent fallback -- a default resource, a
    stripped deployment -- would send a credential somewhere the caller did not
    name. The three checks are re-run here even though the handler already ran
    them, because this function is what actually decides the destination host.

    The api-version is re-minted from the validated value rather than
    concatenated from the caller's query string, so the only bytes that can
    appear after the ``?`` are ``YYYY-MM-DD`` and an optional ``-preview``.
    """
    normalized_resource = normalize_azure_resource(resource)
    if not normalized_resource:
        raise ValueError("azure resource is not a well-formed resource label")
    if not valid_azure_deployment(deployment):
        raise ValueError("azure deployment name is not a safe URL segment")
    normalized_version = normalize_azure_api_version(api_version)
    if not normalized_version:
        raise ValueError("azure api-version is not a well-formed dated version")
    return (f"https://{normalized_resource}.{AZURE_HOST_SUFFIX}"
            f"/openai/deployments/{quote(deployment, safe='-._~')}"
            f"/chat/completions?api-version={normalized_version}")


def normalize_azure_sku(sku: str) -> str:
    """The canonical deployment type, or "" when the value names none.

    "" is never silently equivalent to "undeclared" at the call site: an absent
    header means undeclared (unpriced, no error), while a PRESENT header that
    normalizes to "" is a typo the caller should hear about, because silently
    unpricing a row a customer meant to declare is a revenue leak with no signal.
    """
    squashed = re.sub(r"[^a-z0-9]", "", (sku or "").strip().lower())
    return _SKU_ALIASES.get(squashed, "")


def azure_sku_prices_at_openai_parity(sku: str) -> bool:
    """True only for a deployment type billed at the first-party OpenAI rates."""
    return normalize_azure_sku(sku) in _PARITY_SKUS if sku else False


def azure_priced_model(response_model: str, sku: str) -> str:
    """The priced model for this response UNDER THIS SKU, or "" to leave it unpriced.

    Two independent gates, and both must pass:

      * the SKU must be a declared parity type -- otherwise the OpenAI price
        table is not this deployment's price table and nothing may be billed;
      * the value must be a model identifier we would accept on a receipt.

    Returning "" costs Brevitas the fee on that row and costs the customer
    nothing, which is the direction this module always errs.
    """
    if not azure_sku_prices_at_openai_parity(sku):
        return ""
    value = (response_model or "").strip().lower()
    return value if _MODEL_ID.match(value) else ""


def azure_unpriced_model(deployment: str) -> str:
    """The receipt model for a row this lane will not price.

    Namespaced rather than bare so it can never collide with a MODEL_PRICES key,
    and readable rather than opaque so a dashboard row says WHY it earned
    nothing: the deployment resolved to no priceable model.
    """
    name = deployment if valid_azure_deployment(deployment) else "unknown"
    return f"{AZURE_UNPRICED_PREFIX}{name}"


class AzureStreamModelSniffer:
    """Pull ``model`` out of the first SSE frame that names one. Never raises.

    Azure puts the dated snapshot on EVERY chat-completion chunk, so the model is
    known from chunk 1 -- long before the usage chunk that may or may not arrive
    at the end. Without this, a streamed Azure call would have to fall back to
    the deployment name and price as unpriced even when the SKU was declared.

    NEVER RAISES, by contract, and never alters a byte. It runs inside the
    generator that yields the customer's response, so an exception here would
    truncate a paid, in-flight completion in order to protect a metering read.
    On any fault it latches ``done`` with whatever it had and stops looking.
    """

    #: A stream that produces this much with no newline is not SSE we can read.
    _MAX_BUFFER_BYTES = 64 * 1024

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.model = ""
        self.done = False

    def feed(self, chunk: bytes) -> None:
        if self.done:
            return
        try:
            self._feed(chunk)
        except Exception:
            self.done = True
            self._buffer.clear()

    def _feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            raw, remainder = bytes(self._buffer).split(b"\n", 1)
            self._buffer = bytearray(remainder)
            if self._line(raw.strip()):
                self.done = True
                self._buffer.clear()
                return
        if len(self._buffer) > self._MAX_BUFFER_BYTES:
            self.done = True
            self._buffer.clear()

    def _line(self, raw: bytes) -> bool:
        """True once a model has been found."""
        if raw.startswith(b"data:"):
            raw = raw[5:].strip()
        if not raw or raw == b"[DONE]":
            return False
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(event, dict):
            return False
        value = event.get("model")
        if isinstance(value, str) and value.strip():
            self.model = value.strip()
            return True
        return False
