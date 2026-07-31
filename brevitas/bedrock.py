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
"""
from __future__ import annotations

import re

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
