"""The recorded operation graph, and everything computable from it.

Deliverable 2 of the brief is the recorded graph per run, "so reuse can be recomputed under a
different policy without re-running anything". That is the point of this module: once a run is
recorded, achieved reuse, the theoretical ceiling, the decay curve and the effect of tracking
granularity are all functions of the graph, not of new API calls.

Two ceilings are computed, and the difference between them matters:

  conservative   an operation downstream of a changed artifact is recomputed, and because its
                 output hash then differs, everything downstream of *it* is recomputed too.
                 This is what a pure dependency tracker achieves, so it is the ceiling the
                 arm C health gate is measured against.

  early_cutoff   uses the output hashes actually observed on the repeat run, so an operation
                 that recomputed to a byte-identical result restores its descendants. Build
                 systems call this early cutoff. It can only ever help, so reporting it guards
                 against understating the idea.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from .canon import reuse_key as compute_reuse_key


@dataclass
class Op:
    op_id: str
    kind: str  # "model" | "tool"
    op_type: str
    args: Any
    model: dict[str, Any] | None
    artifacts: dict[str, str]  # artifact id -> content hash, as read by this op
    upstream: list[str]  # op_ids whose outputs this op consumed, in consumption order
    output_hash: str = ""
    reuse_key: str = ""
    tokens: dict[str, int] = field(default_factory=dict)
    wall_ms: int = 0
    retries: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(blob: dict[str, Any]) -> "Op":
        return Op(**blob)


class RunGraph:
    """Operations of one run, in execution order."""

    def __init__(self, run_id: str, ops: Iterable[Op] | None = None) -> None:
        self.run_id = run_id
        self.ops: list[Op] = list(ops or [])
        self._by_id: dict[str, Op] = {op.op_id: op for op in self.ops}

    # -- construction ----------------------------------------------------------------

    def add(self, op: Op, run_root: str | None = None) -> Op:
        """Append an operation and seal its reuse key from its upstreams' output hashes."""
        upstream_hashes = []
        for up in op.upstream:
            if up not in self._by_id:
                raise KeyError(f"{op.op_id} declares unknown upstream {up}")
            upstream_hashes.append(self._by_id[up].output_hash)
        op.reuse_key = compute_reuse_key(
            op_type=op.op_type,
            args=op.args,
            model=op.model,
            artifact_hashes=op.artifacts,
            upstream_output_hashes=upstream_hashes,
            run_root=run_root,
        )
        self.ops.append(op)
        self._by_id[op.op_id] = op
        return op

    def get(self, op_id: str) -> Op:
        return self._by_id[op_id]

    def __len__(self) -> int:
        return len(self.ops)

    # -- structure -------------------------------------------------------------------

    def depths(self) -> dict[str, int]:
        """Longest-path depth of each op. Roots are 0."""
        depth: dict[str, int] = {}
        for op in self.ops:  # execution order is a topological order by construction
            depth[op.op_id] = 0 if not op.upstream else 1 + max(depth[u] for u in op.upstream)
        return depth

    def descendants(self) -> dict[str, set[str]]:
        """Transitive descendants of each op."""
        children: dict[str, list[str]] = {op.op_id: [] for op in self.ops}
        for op in self.ops:
            for up in op.upstream:
                children[up].append(op.op_id)
        out: dict[str, set[str]] = {}
        for op in reversed(self.ops):
            acc: set[str] = set()
            for child in children[op.op_id]:
                acc.add(child)
                acc |= out[child]
            out[op.op_id] = acc
        return out

    def artifact_positions(self) -> dict[str, dict[str, Any]]:
        """Where each artifact sits in the graph.

        Used to verify the perturbation sampler is uniform over graph positions rather than
        favouring roots or leaves - false-positive item 8 and false-negative item 5, which are
        the same check run in opposite directions.

        `depth` is the shallowest op that reads the artifact; `blast` is the number of
        operations that would be recomputed if it changed, which is the quantity that actually
        determines how much reuse a perturbation destroys.
        """
        depth = self.depths()
        desc = self.descendants()
        positions: dict[str, dict[str, Any]] = {}
        for op in self.ops:
            for art in op.artifacts:
                blast = {op.op_id} | desc[op.op_id]
                if art in positions:
                    positions[art]["depth"] = min(positions[art]["depth"], depth[op.op_id])
                    positions[art]["blast_ops"] |= blast
                    positions[art]["readers"].append(op.op_id)
                else:
                    positions[art] = {
                        "depth": depth[op.op_id],
                        "blast_ops": set(blast),
                        "readers": [op.op_id],
                    }
        total = max(len(self.ops), 1)
        for art, info in positions.items():
            info["blast_count"] = len(info["blast_ops"])
            info["blast_fraction"] = len(info["blast_ops"]) / total
            del info["blast_ops"]
        return positions

    # -- invalidation ----------------------------------------------------------------

    def invalidated(self, changed_artifacts: set[str]) -> set[str]:
        """Ops a dependency tracker must recompute, conservatively (no early cutoff).

        An op is recomputed if it reads a changed artifact, or if any upstream was recomputed
        (because recomputation is assumed to change its output hash, which changes this op's
        reuse key transitively).
        """
        dirty: set[str] = set()
        for op in self.ops:
            if any(art in changed_artifacts for art in op.artifacts):
                dirty.add(op.op_id)
            elif any(up in dirty for up in op.upstream):
                dirty.add(op.op_id)
        return dirty

    def invalidated_early_cutoff(
        self, changed_artifacts: set[str], observed_output_hash: dict[str, str]
    ) -> set[str]:
        """As `invalidated`, but an op whose recomputed output matched its stored output does
        not propagate dirtiness to its descendants."""
        dirty: set[str] = set()
        propagates: set[str] = set()
        for op in self.ops:
            direct = any(art in changed_artifacts for art in op.artifacts)
            inherited = any(up in propagates for up in op.upstream)
            if direct or inherited:
                dirty.add(op.op_id)
                if observed_output_hash.get(op.op_id, "\0") != op.output_hash:
                    propagates.add(op.op_id)
        return dirty

    # -- ceilings --------------------------------------------------------------------

    def ceiling(
        self,
        changed_artifacts: set[str],
        cost_of: dict[str, float] | None = None,
        observed_output_hash: dict[str, str] | None = None,
    ) -> dict[str, float]:
        """Theoretical reuse ceiling, count-weighted and cost-weighted.

        Cost weighting is not decoration. A cheap tool call and an expensive model call are not
        one unit each (false-positive item 6), and count-weighted reuse on a run full of file
        reads can look spectacular while saving nothing.
        """
        dirty = self.invalidated(changed_artifacts)
        total_n = len(self.ops)
        reusable_n = total_n - len(dirty)

        costs = cost_of or {}
        total_c = sum(costs.get(op.op_id, 0.0) for op in self.ops)
        reusable_c = sum(costs.get(op.op_id, 0.0) for op in self.ops if op.op_id not in dirty)

        model_ops = [op for op in self.ops if op.kind == "model"]
        model_reusable = sum(1 for op in model_ops if op.op_id not in dirty)
        tool_ops = [op for op in self.ops if op.kind == "tool"]
        tool_reusable = sum(1 for op in tool_ops if op.op_id not in dirty)

        result = {
            "ops_total": total_n,
            "ops_reusable": reusable_n,
            "count_weighted": reusable_n / total_n if total_n else 0.0,
            "cost_total": total_c,
            "cost_reusable": reusable_c,
            "cost_weighted": (reusable_c / total_c) if total_c else 0.0,
            "model_ops_total": len(model_ops),
            "model_ops_reusable": model_reusable,
            "model_count_weighted": (model_reusable / len(model_ops)) if model_ops else 0.0,
            "tool_ops_total": len(tool_ops),
            "tool_ops_reusable": tool_reusable,
            "tool_count_weighted": (tool_reusable / len(tool_ops)) if tool_ops else 0.0,
        }
        if observed_output_hash is not None:
            ec_dirty = self.invalidated_early_cutoff(changed_artifacts, observed_output_hash)
            ec_reusable_c = sum(
                costs.get(op.op_id, 0.0) for op in self.ops if op.op_id not in ec_dirty
            )
            result["early_cutoff_count_weighted"] = (total_n - len(ec_dirty)) / total_n if total_n else 0.0
            result["early_cutoff_cost_weighted"] = (ec_reusable_c / total_c) if total_c else 0.0
        return result

    # -- granularity ------------------------------------------------------------------

    def coarsened(self, mapping: dict[str, str]) -> "RunGraph":
        """A copy of this graph with artifact ids rewritten, e.g. every span in a file mapped
        to the file itself.

        Comparing reuse on the fine graph against reuse on the coarse one is the explicit test
        of tracking granularity demanded by false-negative item 2: over-recording edges - a
        whole file treated as an input when one function was read - invalidates operations that
        should have survived, and looks identical to the idea failing.
        """
        clone = RunGraph(self.run_id + ":coarse")
        for op in self.ops:
            coarse_arts: dict[str, str] = {}
            for art, digest in op.artifacts.items():
                key = mapping.get(art, art)
                # A coarse artifact's hash is the combination of its parts' hashes, so any part
                # changing changes the coarse hash. That is exactly the over-invalidation being
                # measured.
                coarse_arts[key] = (coarse_arts.get(key, "") + digest)[:512]
            clone.ops.append(
                Op(
                    op_id=op.op_id,
                    kind=op.kind,
                    op_type=op.op_type,
                    args=op.args,
                    model=op.model,
                    artifacts=coarse_arts,
                    upstream=list(op.upstream),
                    output_hash=op.output_hash,
                    reuse_key=op.reuse_key,
                    tokens=dict(op.tokens),
                )
            )
        clone._by_id = {op.op_id: op for op in clone.ops}
        return clone

    # -- persistence ------------------------------------------------------------------

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"run_id": self.run_id, "ops": [op.to_json() for op in self.ops]},
                handle,
                indent=1,
                sort_keys=True,
            )

    @staticmethod
    def load(path: str) -> "RunGraph":
        with open(path, encoding="utf-8") as handle:
            blob = json.load(handle)
        return RunGraph(blob["run_id"], [Op.from_json(o) for o in blob["ops"]])
