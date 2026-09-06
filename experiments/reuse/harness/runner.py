"""Executes one run of one family under one arm.

The tool layer is the harness's own, so every dependency edge is recorded at the moment the
prompt is assembled rather than reconstructed from a transcript afterwards. Reconstruction loses
edges, and a missed edge looks like independence, which overstates reuse.

Accounting note. Arms B and C pay the same "day one" cost, because arm B also has to perform the
first run - it simply does not keep the results. So the marginal metric is computed over the
*repeat* run, and the store population cost is not charged to arm C. What is charged to arm C is
the ongoing storage of the store and the shadow recomputation needed to bound staleness, because
both are real costs a production deployment would carry.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .canon import reuse_key as compute_reuse_key, sha256_text
from .costmodel import Cost, op_cost
from .graph import Op, RunGraph
from .model import ModelResult, call as model_call, new_session_id
from .store import ReuseStore


@dataclass
class OpSpec:
    """One planned operation.

    `session_group` controls provider-cache structure: operations sharing a group run as one
    conversation, so the accumulating prefix is cached. Independent operations get a fresh
    session each and rely on the shared system prompt for their cache hit - which the arm B
    tuning probe established is the cheap configuration (91.8% cache-read fraction across
    separate sessions, versus 0% with the same content in the user message).
    """

    op_id: str
    kind: str  # "model" | "tool"
    op_type: str
    args: dict[str, Any]
    upstream: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    session_group: str | None = None
    system: str | None = None  # cacheable content identical across operations
    # (artifacts, upstream_outputs) -> prompt text, for model ops
    prompt_fn: Callable[[dict[str, str], dict[str, str]], str] | None = None
    # (artifacts, upstream_outputs) -> output text, for tool ops
    tool_fn: Callable[[dict[str, str], dict[str, str]], str] | None = None
    is_final: bool = False


@dataclass
class RunResult:
    run_id: str
    arm: str
    graph: RunGraph
    outputs: dict[str, str]
    cost: Cost
    ops_executed: int
    ops_served: int
    model_ops_executed: int
    model_ops_served: int
    tool_ops_executed: int
    tool_ops_served: int
    served_cost_if_recomputed: float
    hit_records: list[dict[str, Any]]
    shadow_records: list[dict[str, Any]]
    shadow_cost: Cost
    model_versions: set[str]
    retries: int
    first_op_cache_read_tokens: int
    wall_seconds: float
    per_op_cost: dict[str, float]

    @property
    def cache_read_fraction(self) -> float:
        """Arm B health gate quantity, pooled over the run's model calls."""
        return self._read_frac

    _read_frac: float = 0.0


def _estimate_recompute_cost(spec: OpSpec, reference: dict[str, float]) -> float:
    """What a served operation would have cost had it been executed.

    Taken from the same operation's measured cost in the reference (arm B) run of the same cell,
    so it is a measured number rather than a guess. Falls back to the run's mean model-op cost
    when the reference lacks the op.
    """
    if spec.op_id in reference:
        return reference[spec.op_id]
    if spec.kind == "tool":
        return 0.0
    values = [v for v in reference.values() if v > 0]
    return (sum(values) / len(values)) if values else 0.0


def run_once(
    *,
    family: Any,
    artifacts: dict[str, str],
    arm: str,
    model: str,
    run_id: str,
    cwd: str,
    store: ReuseStore | None = None,
    write_store: bool = False,
    shadow_rate: float = 0.0,
    shadow_seed: int = 0,
    reference_costs: dict[str, float] | None = None,
) -> RunResult:
    """Execute one run.

    `store` is consulted only in arm C. `write_store` populates it (the day-one run). Arms A and B
    never read it, and arm C sits on exactly the same provider-cache configuration as arm B, so
    the only difference between them is the store.
    """
    started = time.time()
    graph = RunGraph(run_id)
    outputs: dict[str, str] = {}
    cost = Cost()
    shadow_cost = Cost()
    hit_records: list[dict[str, Any]] = []
    shadow_records: list[dict[str, Any]] = []
    per_op_cost: dict[str, float] = {}
    model_versions: set[str] = set()
    sessions: dict[str, str] = {}
    session_started: set[str] = set()
    reference = reference_costs or {}
    rng = random.Random(f"shadow-{run_id}-{shadow_seed}")

    ops_executed = ops_served = 0
    model_exec = model_served = tool_exec = tool_served = 0
    served_cost_if_recomputed = 0.0
    retries = 0
    first_op_cache_read = -1
    read_tokens_total = 0
    input_tokens_total = 0

    for spec in family.plan(artifacts):
        art_hashes = {a: sha256_text(artifacts[a]) for a in spec.artifacts}
        upstream_outputs = {u: outputs[u] for u in spec.upstream}
        upstream_hashes = [graph.get(u).output_hash for u in spec.upstream]

        key = compute_reuse_key(
            op_type=spec.op_type,
            args=spec.args,
            model={"id": model} if spec.kind == "model" else None,
            artifact_hashes=art_hashes,
            upstream_output_hashes=upstream_hashes,
        )

        # ---- arm C: try the store -------------------------------------------------
        served = None
        if arm == "C" and store is not None:
            served = store.get(key, read_by_run=run_id)

        if served is not None:
            outputs[spec.op_id] = served["output"]
            op = Op(
                op_id=spec.op_id, kind=spec.kind, op_type=spec.op_type, args=spec.args,
                model={"id": model} if spec.kind == "model" else None,
                artifacts=art_hashes, upstream=list(spec.upstream),
                output_hash=served["output_hash"], tokens={}, wall_ms=0,
            )
            graph.ops.append(op)
            graph._by_id[op.op_id] = op
            op.reuse_key = key

            would_have_cost = _estimate_recompute_cost(spec, reference)
            served_cost_if_recomputed += would_have_cost
            per_op_cost[spec.op_id] = 0.0
            ops_served += 1
            if spec.kind == "model":
                model_served += 1
            else:
                tool_served += 1
            hit_records.append({
                "op_id": spec.op_id, "kind": spec.kind, "op_type": spec.op_type,
                "cross_run": served["cross_run"], "written_by_run": served["written_by_run"],
                "age_seconds": time.time() - served["written_at"],
                "cost_if_recomputed_usd": would_have_cost,
            })

            # ---- shadow computation, excluded from all cost totals ----------------
            if spec.kind == "model" and shadow_rate > 0 and rng.random() < shadow_rate:
                prompt = spec.prompt_fn(artifacts, upstream_outputs)
                fresh = model_call(
                    prompt, model=model, arm=arm, cwd=cwd, session_id=new_session_id(),
                    system=spec.system,
                )
                shadow_cost = shadow_cost + op_cost(
                    model_version=fresh.model_version,
                    input_tokens=fresh.input_tokens, output_tokens=fresh.output_tokens,
                    ephemeral_5m_tokens=fresh.ephemeral_5m_tokens,
                    ephemeral_1h_tokens=fresh.ephemeral_1h_tokens,
                    cache_read_tokens=fresh.cache_read_tokens,
                )
                shadow_records.append({
                    "op_id": spec.op_id,
                    "exact_match": fresh.text.strip() == served["output"].strip(),
                    "served_hash": served["output_hash"],
                    "fresh_hash": sha256_text(fresh.text.strip()),
                    "served": served["output"][:600],
                    "fresh": fresh.text[:600],
                })
            continue

        # ---- execute --------------------------------------------------------------
        if spec.kind == "tool":
            text = spec.tool_fn(artifacts, upstream_outputs)
            tokens: dict[str, int] = {}
            op_usd = 0.0
            tool_exec += 1
        else:
            prompt = spec.prompt_fn(artifacts, upstream_outputs)
            group = spec.session_group
            if group:
                sid = sessions.setdefault(group, new_session_id())
                resume = group in session_started
                session_started.add(group)
            else:
                sid, resume = new_session_id(), False
            result: ModelResult = model_call(
                prompt, model=model, arm=arm, cwd=cwd, session_id=sid, resume=resume,
                system=spec.system,
            )
            text = result.text
            retries += result.retries
            model_versions.add(result.model_version)
            read_tokens_total += result.cache_read_tokens
            input_tokens_total += result.billable_input_tokens
            if first_op_cache_read < 0:
                first_op_cache_read = result.cache_read_tokens
            this = op_cost(
                model_version=result.model_version,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                ephemeral_5m_tokens=result.ephemeral_5m_tokens,
                ephemeral_1h_tokens=result.ephemeral_1h_tokens,
                cache_read_tokens=result.cache_read_tokens,
            )
            cost = cost + this
            op_usd = this.total
            tokens = {
                "input": result.input_tokens, "output": result.output_tokens,
                "cache_write_5m": result.ephemeral_5m_tokens,
                "cache_write_1h": result.ephemeral_1h_tokens,
                "cache_read": result.cache_read_tokens,
            }
            model_exec += 1

        text = text.strip()
        outputs[spec.op_id] = text
        per_op_cost[spec.op_id] = op_usd
        ops_executed += 1

        op = Op(
            op_id=spec.op_id, kind=spec.kind, op_type=spec.op_type, args=spec.args,
            model={"id": model} if spec.kind == "model" else None,
            artifacts=art_hashes, upstream=list(spec.upstream), tokens=tokens,
        )
        op.output_hash = sha256_text(text)
        graph.ops.append(op)
        graph._by_id[op.op_id] = op
        op.reuse_key = key

        if write_store and store is not None:
            store.put(
                reuse_key=key, op_type=spec.op_type, kind=spec.kind, output=text,
                output_hash=op.output_hash, tokens=tokens, run_id=run_id,
                family=getattr(family, "name", "?"),
            )

    result = RunResult(
        run_id=run_id, arm=arm, graph=graph, outputs=outputs, cost=cost,
        ops_executed=ops_executed, ops_served=ops_served,
        model_ops_executed=model_exec, model_ops_served=model_served,
        tool_ops_executed=tool_exec, tool_ops_served=tool_served,
        served_cost_if_recomputed=served_cost_if_recomputed,
        hit_records=hit_records, shadow_records=shadow_records, shadow_cost=shadow_cost,
        model_versions=model_versions, retries=retries,
        first_op_cache_read_tokens=max(0, first_op_cache_read),
        wall_seconds=time.time() - started, per_op_cost=per_op_cost,
    )
    result._read_frac = (read_tokens_total / input_tokens_total) if input_tokens_total else 0.0
    return result
