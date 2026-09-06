"""Model operations, metered from real response usage fields.

The brief requires token counts to come from the API response usage object and never from a
tokenizer estimate, and it requires dollars rather than tokens because the cache-write
multiplier can invert the sign of a token-only comparison. Both are satisfied here by driving
`claude -p --output-format json`, which returns the genuine per-request usage block:

    usage.input_tokens
    usage.output_tokens
    usage.cache_creation_input_tokens
    usage.cache_read_input_tokens
    usage.cache_creation.ephemeral_5m_input_tokens
    usage.cache_creation.ephemeral_1h_input_tokens

The 5m/1h split matters: the extended tier is written at a higher multiplier, so arm B "tuned
as hard as you can make it" is not automatically arm B "cheapest".

Arm control, all of it verified from usage rather than from configuration:

    A   DISABLE_PROMPT_CACHING=1     no caching at all, context row only
    B   ENABLE_PROMPT_CACHING_1H=1   provider caching on the extended tier
    C   same as B, plus the reuse layer skipping operations before they reach here

Tools are disabled on every call. The agent's tool layer is the harness's own (see runner.py),
so every dependency edge is recorded live at the moment it is created rather than reconstructed
from a transcript afterwards, which is where edges get lost.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

CLI = os.environ.get("REUSE_CLAUDE_BIN", "claude")

# A neutral system prompt. Kept fixed across arms so it is a constant additive overhead rather
# than a confound, and measured by `measure_overhead` so the report can state how much of every
# call is harness rather than workload.
SYSTEM_PROMPT = (
    "You are a precise analysis engine operating inside a benchmark harness. "
    "Follow the output format in the user message exactly. "
    "Emit no preamble, no explanation and no commentary unless explicitly asked for one."
)


class ModelError(RuntimeError):
    pass


@dataclass
class ModelResult:
    text: str
    model_version: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    ephemeral_5m_tokens: int
    ephemeral_1h_tokens: int
    reported_cost_usd: float
    service_tier: str
    duration_api_ms: int
    retries: int = 0
    retry_log: list[str] = field(default_factory=list)
    session_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def billable_input_tokens(self) -> int:
        """Every input token the request was charged for, in any class."""
        return self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens

    @property
    def cache_read_fraction(self) -> float:
        """The arm B health gate quantity: cache-read tokens as a share of all input tokens,
        read straight off the response rather than inferred from configuration."""
        total = self.billable_input_tokens
        return (self.cache_read_tokens / total) if total else 0.0

    def to_json(self) -> dict[str, Any]:
        blob = asdict(self)
        blob.pop("raw", None)
        return blob


def arm_env(arm: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("DISABLE_PROMPT_CACHING", None)
    env.pop("ENABLE_PROMPT_CACHING_1H", None)
    if arm == "A":
        env["DISABLE_PROMPT_CACHING"] = "1"
    else:
        # Arm B is required to be tuned as hard as possible, so the extended tier is on. Arm C
        # sits on top of exactly the same configuration; if it did not, the comparison would be
        # measuring the TTL change rather than the reuse layer.
        env["ENABLE_PROMPT_CACHING_1H"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def new_session_id() -> str:
    return str(uuid.uuid4())


def call(
    prompt: str,
    *,
    model: str,
    arm: str,
    cwd: str,
    session_id: str | None = None,
    resume: bool = False,
    system: str | None = None,
    max_retries: int = 3,
    timeout_s: int = 900,
) -> ModelResult:
    """One model operation. Multi-turn is expressed by reusing `session_id` with `resume=True`,
    which is what gives arm B a long repeated prefix to cache.

    `system` carries the content that is identical across operations. That placement is not
    cosmetic: measured on this CLI path, a shared prefix in the system prompt is read from cache
    across separate sessions at a 91.8% read fraction, while the same content in the user message
    is rewritten at the 2.0x cache-write rate on every single request. Same tokens, about 7.9x the
    dollars. Arm B is tuned with the cheap placement.
    """
    session_id = session_id or new_session_id()
    argv = [
        CLI,
        "-p",
        "--output-format", "json",
        "--model", model,
        "--disallowed-tools", "*",
        "--system-prompt", system or SYSTEM_PROMPT,
        "--exclude-dynamic-system-prompt-sections",
        # No --permission-mode: "plan" injects a Plan Mode system reminder that hijacks the model
        # into writing a plan instead of answering, and inflates output tokens by roughly 10x.
        # Tools are already fully disabled above.
    ]
    if resume:
        argv += ["--resume", session_id]
    else:
        argv += ["--session-id", session_id]

    retries = 0
    retry_log: list[str] = []
    last_error = ""
    while retries <= max_retries:
        started = time.time()
        try:
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=cwd,
                env=arm_env(arm),
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            last_error = f"timeout after {timeout_s}s"
            retry_log.append(last_error)
            retries += 1
            time.sleep(min(60, 5 * 2**retries))
            continue

        if proc.returncode != 0:
            last_error = f"exit {proc.returncode}: {proc.stderr.strip()[:400]}"
            retry_log.append(last_error)
            retries += 1
            time.sleep(min(60, 5 * 2**retries))
            continue

        try:
            blob = json.loads(proc.stdout)
        except json.JSONDecodeError:
            last_error = f"unparseable stdout: {proc.stdout[:300]!r}"
            retry_log.append(last_error)
            retries += 1
            continue

        if blob.get("is_error") or blob.get("subtype") != "success":
            last_error = f"api error: {blob.get('api_error_status')} {str(blob.get('result'))[:200]}"
            retry_log.append(last_error)
            retries += 1
            time.sleep(min(60, 5 * 2**retries))
            continue

        usage = blob.get("usage", {}) or {}
        creation = usage.get("cache_creation", {}) or {}
        model_usage = blob.get("modelUsage", {}) or {}
        # The exact model version string, recorded per call so a mid-matrix model change can be
        # detected and the affected cells marked suspect.
        version = next(iter(model_usage), model)

        return ModelResult(
            text=blob.get("result", ""),
            model_version=version,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
            ephemeral_5m_tokens=int(creation.get("ephemeral_5m_input_tokens", 0)),
            ephemeral_1h_tokens=int(creation.get("ephemeral_1h_input_tokens", 0)),
            reported_cost_usd=float(blob.get("total_cost_usd", 0.0)),
            service_tier=str(usage.get("service_tier", "")),
            duration_api_ms=int(blob.get("duration_api_ms", int((time.time() - started) * 1000))),
            retries=retries,
            retry_log=retry_log,
            session_id=session_id,
            raw=blob,
        )

    raise ModelError(f"model call failed after {max_retries} retries: {last_error}")


def measure_overhead(model: str, cwd: str) -> dict[str, Any]:
    """How many input tokens every call costs before any workload content is added.

    This is harness overhead, not workload. It inflates both arm B and arm C by the same
    additive amount per surviving call, which dilutes the marginal saving toward zero. Reporting
    it lets a reader correct for it instead of taking the diluted figure at face value.
    """
    probe = call("Reply with exactly: OK", model=model, arm="B", cwd=cwd)
    return {
        "prompt_chars": len("Reply with exactly: OK"),
        "input_tokens_for_near_empty_prompt": probe.billable_input_tokens,
        "model_version": probe.model_version,
        "note": "constant additive per-call input cost attributable to the harness, not the workload",
    }
