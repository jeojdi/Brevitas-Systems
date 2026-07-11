"""Provider usage receipts normalized without retaining prompt or response content."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from token_efficiency_model.lossless.provider_cache import count_tokens


@dataclass(frozen=True)
class TokenReceipt:
    fresh_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    @property
    def input_tokens(self) -> int:
        return self.fresh_input_tokens + self.cached_input_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int]:
        return {**asdict(self), "input_tokens": self.input_tokens,
                "total_tokens": self.total_tokens}


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage(usage: dict | None, provider: str = "") -> TokenReceipt:
    """Parse common provider receipts; unknown shapes remain zero/unpriced."""
    if not isinstance(usage, dict):
        return TokenReceipt()
    if isinstance(usage.get("usage"), dict):
        usage = usage["usage"]
    elif isinstance(usage.get("usageMetadata"), dict):
        usage = usage["usageMetadata"]
    elif isinstance(usage.get("token_usage"), dict):
        usage = usage["token_usage"]
    elif isinstance(usage.get("meta"), dict) and isinstance(usage["meta"].get("billed_units"), dict):
        usage = usage["meta"]

    billed = usage.get("billed_units") or {}
    if isinstance(billed, dict) and billed:
        return TokenReceipt(_int(billed.get("input_tokens")), 0, 0,
                            _int(billed.get("output_tokens")))

    # Anthropic Messages.
    if provider.lower() == "anthropic" or any(
        key in usage for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
    ):
        return TokenReceipt(
            _int(usage.get("input_tokens")),
            _int(usage.get("cache_read_input_tokens")),
            _int(usage.get("cache_creation_input_tokens")),
            _int(usage.get("output_tokens")),
        )

    # OpenAI Responses.
    if "input_tokens" in usage or "output_tokens" in usage:
        details = usage.get("input_tokens_details") or {}
        cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        total_input = _int(usage.get("input_tokens"))
        # Cohere v2 puts authoritative counts under billed_units.
        output = _int(usage.get("output_tokens"))
        return TokenReceipt(max(0, total_input - cached), cached, 0, output)

    # OpenAI-compatible Chat Completions.
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        details = usage.get("prompt_tokens_details") or {}
        cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        cached = cached or _int(usage.get("prompt_cache_hit_tokens"))
        prompt = _int(usage.get("prompt_tokens"))
        return TokenReceipt(max(0, prompt - cached), cached, 0,
                            _int(usage.get("completion_tokens")))

    # Gemini / Vertex usageMetadata.
    if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
        prompt = _int(usage.get("promptTokenCount"))
        cached = _int(usage.get("cachedContentTokenCount"))
        return TokenReceipt(max(0, prompt - cached), cached, 0,
                            _int(usage.get("candidatesTokenCount")))

    # Bedrock Converse and invoke-model normalized receipts.
    if "inputTokens" in usage or "outputTokens" in usage:
        return TokenReceipt(_int(usage.get("inputTokens")), 0, 0,
                            _int(usage.get("outputTokens")))

    tokens = usage.get("tokens") or {}
    if isinstance(tokens, dict):
        receipt = TokenReceipt(_int(tokens.get("input_tokens")), 0, 0,
                               _int(tokens.get("output_tokens")))
        if receipt.total_tokens:
            return receipt

    # Ollama and several local OpenAI-compatible runtimes.
    if "prompt_eval_count" in usage or "eval_count" in usage:
        return TokenReceipt(_int(usage.get("prompt_eval_count")), 0, 0,
                            _int(usage.get("eval_count")))

    # Replicate and other hosted inference receipts commonly use this spelling.
    metrics = usage.get("metrics") if isinstance(usage.get("metrics"), dict) else usage
    if "input_token_count" in metrics or "output_token_count" in metrics:
        return TokenReceipt(_int(metrics.get("input_token_count")), 0, 0,
                            _int(metrics.get("output_token_count")))
    return TokenReceipt()


def merge_receipts(left: TokenReceipt, right: TokenReceipt) -> TokenReceipt:
    """Streaming usage values are normally cumulative, so keep each largest value."""
    return TokenReceipt(*(max(a, b) for a, b in zip(
        asdict(left).values(), asdict(right).values()
    )))


class SSEUsageParser:
    """Tee an SSE/JSONL stream while extracting only its final numeric usage receipt."""

    def __init__(self, provider: str = "") -> None:
        self.provider = provider
        self.receipt = TokenReceipt()
        self.response_id = ""
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            self._line(raw.rstrip(b"\r"))

    def finish(self) -> TokenReceipt:
        if self._buffer:
            self._line(self._buffer.rstrip(b"\r"))
            self._buffer = b""
        return self.receipt

    def _line(self, raw: bytes) -> None:
        if not raw:
            return
        if raw.startswith(b"data:"):
            raw = raw[5:].strip()
        if not raw or raw == b"[DONE]":
            return
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(event, dict):
            return
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        self.response_id = str(
            response.get("id") or message.get("id") or event.get("id") or self.response_id
        )
        candidates = [event.get("usage"), response.get("usage"), message.get("usage")]
        for usage in candidates:
            if isinstance(usage, dict):
                self.receipt = merge_receipts(
                    self.receipt, normalize_usage(usage, self.provider)
                )


# Standard/global on-demand USD per million tokens, verified 2026-07-10. Costs are
# snapshotted into each event at ingestion; unknown IDs intentionally have no fallback.
PRICING_VERSION = "2026-07-10"
MODEL_PRICES: dict[tuple[str, str], dict[str, float]] = {
    ("anthropic", "claude-opus-4-8"): {"input": 5.0, "cached": .5, "write": 6.25, "output": 25.0},
    ("anthropic", "claude-opus-4-7"): {"input": 5.0, "cached": .5, "write": 6.25, "output": 25.0},
    ("anthropic", "claude-opus-4-6"): {"input": 5.0, "cached": .5, "write": 6.25, "output": 25.0},
    ("anthropic", "claude-sonnet-4-6"): {"input": 3.0, "cached": .3, "write": 3.75, "output": 15.0},
    ("anthropic", "claude-haiku-4-5-20251001"): {"input": 1.0, "cached": .1, "write": 1.25, "output": 5.0},
    ("openai", "gpt-4o"): {"input": 2.5, "cached": 1.25, "write": 2.5, "output": 10.0},
    ("openai", "gpt-4o-mini"): {"input": .15, "cached": .075, "write": .15, "output": .6},
    ("openai", "o3-mini"): {"input": 1.1, "cached": .55, "write": 1.1, "output": 4.4},
    ("deepseek", "deepseek-chat"): {"input": .14, "cached": .0028, "write": .14, "output": .28},
    ("deepseek", "deepseek-reasoner"): {"input": .14, "cached": .0028, "write": .14, "output": .28},
    ("deepseek", "deepseek-v4-flash"): {"input": .14, "cached": .0028, "write": .14, "output": .28},
    ("deepseek", "deepseek-v4-pro"): {"input": .435, "cached": .003625, "write": .435, "output": .87},
}


def canonical_provider(provider: str, model: str) -> str:
    p, m = (provider or "").lower(), (model or "").lower()
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return {"azure_openai": "openai"}.get(p, p)


def calculate_costs(provider: str, model: str, baseline_input_tokens: int,
                    receipt: TokenReceipt, baseline_output_tokens: int | None = None) -> dict[str, Any]:
    provider = canonical_provider(provider, model)
    price = MODEL_PRICES.get((provider, model))
    if not price:
        return {"pricing_status": "unpriced", "baseline_cost_usd": None,
                "actual_cost_usd": None, "measured_savings_usd": None,
                "pricing_version": "", "prices": {}}
    million = 1_000_000
    baseline_output = receipt.output_tokens if baseline_output_tokens is None else max(0, baseline_output_tokens)
    baseline = (max(0, baseline_input_tokens) * price["input"]
                + baseline_output * price["output"]) / million
    actual = (
        receipt.fresh_input_tokens * price["input"]
        + receipt.cached_input_tokens * price["cached"]
        + receipt.cache_write_tokens * price["write"]
        + receipt.output_tokens * price["output"]
    ) / million
    return {
        "pricing_status": "priced",
        "baseline_cost_usd": round(baseline, 10),
        "actual_cost_usd": round(actual, 10),
        "measured_savings_usd": round(baseline - actual, 10),
        "pricing_version": PRICING_VERSION,
        "prices": dict(price),
    }


def count_request_tokens(body: dict, operation: str = "") -> int:
    """Local baseline count; provider receipt remains authoritative for actual usage."""
    chunks: list[str] = []

    def add_content(value: Any) -> None:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    if item.get("type") in ("text", "input_text", "output_text"):
                        add_content(item.get("text"))
                    elif "content" in item:
                        add_content(item.get("content"))
                elif isinstance(item, str):
                    chunks.append(item)
        elif isinstance(value, dict):
            add_content(value.get("content"))

    add_content(body.get("system"))
    add_content(body.get("instructions"))
    add_content(body.get("input"))
    add_content(body.get("prompt"))
    for message in body.get("messages") or []:
        if isinstance(message, dict):
            add_content(message.get("content"))
    if body.get("tools"):
        chunks.append(json.dumps(body["tools"], separators=(",", ":"), sort_keys=True))
    return sum(count_tokens(chunk) for chunk in chunks)
