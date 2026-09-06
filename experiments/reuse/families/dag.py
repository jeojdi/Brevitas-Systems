"""Family: layered DAG with early cutoff — the steel-man shape.

Scaled-down implementation of the workload a blind agent designed when asked to make cross-run
dependency-tracked reuse perform as well as possible, subject only to being something a real
customer would pay for repeatedly (see `family4_spec.md`). Its reference scale is 6,810
operations; this is a fixed-factor reduction, and reuse is reported as a function of run size so
the scaling is visible rather than assumed.

The three mechanisms the designer relied on are all preserved here:

  bounded blast radius   per-file fact extraction is fan-in 1, so one changed file dirties its own
                         extraction and a bounded set of descendants, not the whole run.
  early cutoff           facts are extracted into a canonical, prose-free JSON form, so a change
                         that alters no analysis-relevant fact re-runs exactly one operation,
                         produces a byte-identical output, and stops dead.
  narrow aggregation     the roll-up is a balanced binary reduce tree, so one changed leaf dirties
                         log2(n) nodes rather than a monolithic summariser.

Ground truth is exact and free: the generator plants the defects, so it knows every finding by
construction and the grader is an F1 against a known set.
"""

from __future__ import annotations

import json
import math
import random

from ..harness.runner import OpSpec

NAME = "dag"
SHAPE = "layered-DAG"

_RULES = "\n".join(
    f"  A{i:03d}. {text}"
    for i, text in enumerate(
        [
            "A SOURCE line marks data entering from an untrusted origin.",
            "A SINK line marks data reaching a dangerous operation.",
            "A SANITIZE line neutralises untrusted data for every sink below it in the same file.",
            "A SECRET line is a hardcoded credential and is always reportable.",
            "A file has a taint finding when it contains a SOURCE, then a SINK below it, with no "
            "SANITIZE line between them.",
            "Line numbers are 1-based and count every line in the file including the header.",
            "Report facts exactly as they appear; never infer a fact that is not written.",
            "Ignore NOTE lines entirely; they carry no analysis-relevant meaning.",
            "Ignore blank lines and any line beginning with a hash character.",
            "Preserve the order in which facts appear in the file.",
        ]
        * 24
    )
)

EXTRACT_SYSTEM = f"""You extract security-relevant facts from source files.

RULES

{_RULES}

OUTPUT FORMAT

A single line of compact JSON with exactly these keys, in this order:
  {{"sources":[<line numbers>],"sinks":[<line numbers>],"sanitizers":[<line numbers>],"secrets":[<line numbers>]}}
Line numbers are integers in ascending order. Emit nothing else: no preamble, no code fence."""

LEDGER_SYSTEM = f"""You assemble a findings ledger from per-file security facts.

RULES

{_RULES}

OUTPUT FORMAT

A single line of compact JSON: {{"findings":[{{"file":"<path>","kind":"TAINT"|"SECRET","line":<int>}}]}}
Findings are sorted by file then line. A TAINT finding's line is the sink's line number. A SECRET
finding's line is the secret's line number. Emit nothing else."""

_LINE_KINDS = ("SOURCE", "SINK", "SANITIZE", "SECRET", "NOTE")


def generate(seed: int, size: int) -> dict[str, str]:
    """`size` source files across `size // 4` modules. Deterministic in `seed`."""
    rng = random.Random(f"dag-{seed}")
    artifacts: dict[str, str] = {}
    modules = max(1, size // 4)
    for i in range(size):
        module = i % modules
        lines = [f"# file {i:03d} module {module:02d}"]
        # A seeded but genuinely varied body, so extraction is a real task rather than a template
        # match, while the planted facts stay exactly known to the generator.
        plan = rng.choice([
            ["SOURCE", "NOTE", "SINK"],            # taint
            ["SOURCE", "SANITIZE", "SINK"],        # safe
            ["NOTE", "SINK", "NOTE"],              # sink with no source
            ["SOURCE", "NOTE", "NOTE"],            # source with no sink
            ["SECRET", "NOTE", "SOURCE", "SINK"],  # secret plus taint
            ["NOTE", "NOTE", "NOTE"],              # clean
        ])
        for kind in plan:
            if kind == "SOURCE":
                lines.append(f"SOURCE request_param_{rng.randrange(100, 999)}")
            elif kind == "SINK":
                lines.append(f"SINK db_execute_{rng.randrange(100, 999)}")
            elif kind == "SANITIZE":
                lines.append(f"SANITIZE escape_{rng.randrange(100, 999)}")
            elif kind == "SECRET":
                lines.append(f'SECRET api_key = "sk_{rng.randrange(10000, 99999)}"')
            else:
                lines.append(f"NOTE reviewed by team {rng.randrange(1, 9)}")
        artifacts[f"src/mod{module:02d}/file{i:03d}.txt"] = "\n".join(lines) + "\n"
    return artifacts


def _facts(text: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {"sources": [], "sinks": [], "sanitizers": [], "secrets": []}
    for n, line in enumerate(text.splitlines(), start=1):
        if line.startswith("SOURCE"):
            out["sources"].append(n)
        elif line.startswith("SINK"):
            out["sinks"].append(n)
        elif line.startswith("SANITIZE"):
            out["sanitizers"].append(n)
        elif line.startswith("SECRET"):
            out["secrets"].append(n)
    return out


def _findings_for(path: str, text: str) -> list[dict[str, object]]:
    f = _facts(text)
    found: list[dict[str, object]] = []
    for line in f["secrets"]:
        found.append({"file": path, "kind": "SECRET", "line": line})
    for sink in f["sinks"]:
        sources_above = [s for s in f["sources"] if s < sink]
        if not sources_above:
            continue
        latest_source = max(sources_above)
        if any(latest_source < s < sink for s in f["sanitizers"]):
            continue
        found.append({"file": path, "kind": "TAINT", "line": sink})
    return found


def answer_key(artifacts: dict[str, str]) -> dict[str, object]:
    """Every finding, known by construction because the generator planted them."""
    findings: list[dict[str, object]] = []
    for path in sorted(artifacts):
        findings.extend(_findings_for(path, artifacts[path]))
    key: dict[str, object] = {
        f"{f['file']}|{f['kind']}|{f['line']}": True for f in findings
    }
    key["_findings"] = findings
    return key


def _files(artifacts: dict[str, str]) -> list[str]:
    return sorted(a for a in artifacts if a.startswith("src/"))


# Set by `plan` so the ledger prompt closure can name file paths without threading the artifact
# map through every intermediate layer.
_arts_cache: dict[str, str] = {}


def plan(artifacts: dict[str, str]) -> list[OpSpec]:
    global _arts_cache
    _arts_cache = artifacts
    files = _files(artifacts)
    modules: dict[str, list[int]] = {}
    for i, path in enumerate(files):
        modules.setdefault(path.split("/")[1], []).append(i)

    specs: list[OpSpec] = []

    # Layer 1: read each file. Tool ops, fan-in 1, the roots of the graph.
    for i, path in enumerate(files):
        specs.append(OpSpec(
            op_id=f"read{i:03d}", kind="tool", op_type="read_file",
            args={"path": path}, artifacts=[path],
            tool_fn=lambda arts, _ups, p=path: arts[p],
        ))

    # Layer 2: extract canonical facts. The expensive, embarrassingly parallel layer, and the one
    # where early cutoff fires - a cosmetic edit re-runs one op and produces identical output.
    for i, path in enumerate(files):
        specs.append(OpSpec(
            op_id=f"facts{i:03d}", kind="model", op_type="extract_facts",
            args={"path": path, "op_version": 1}, artifacts=[path], upstream=[f"read{i:03d}"],
            system=EXTRACT_SYSTEM,
            prompt_fn=lambda arts, ups, p=path: (
                "FILE: " + p + "\n\n" + list(ups.values())[0] + "\nEmit the facts JSON."
            ),
        ))

    # Layer 3: per-module roll-up. Tool ops, narrow fan-in.
    module_ids = sorted(modules)
    for m, module in enumerate(module_ids):
        members = modules[module]
        specs.append(OpSpec(
            op_id=f"module{m:02d}", kind="tool", op_type="module_rollup",
            args={"module": module}, upstream=[f"facts{i:03d}" for i in members],
            tool_fn=lambda _arts, ups: json.dumps(dict(sorted(ups.items())), sort_keys=True),
        ))

    # Layer 4: balanced binary reduce tree, so one changed leaf dirties log2(n) nodes rather than
    # a single monolithic aggregation node.
    level = [f"module{m:02d}" for m in range(len(module_ids))]
    node = 0
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            pair = level[i:i + 2]
            op_id = f"reduce{node:03d}"
            node += 1
            specs.append(OpSpec(
                op_id=op_id, kind="tool", op_type="reduce",
                args={"arity": len(pair)}, upstream=list(pair),
                tool_fn=lambda _arts, ups: json.dumps(dict(sorted(ups.items())), sort_keys=True),
            ))
            nxt.append(op_id)
        level = nxt

    # Layer 5: emit the ledger. One model op consuming the reduce root.
    specs.append(OpSpec(
        op_id="ledger", kind="model", op_type="emit_ledger",
        args={"n_files": len(files)}, upstream=level[:1] or [],
        system=LEDGER_SYSTEM,
        prompt_fn=lambda _arts, ups: (
            "PER-FILE FACTS (json, keyed by operation):\n"
            + (list(ups.values())[0] if ups else "{}")
            + "\n\nFILE PATHS IN ORDER:\n"
            + "\n".join(f"  facts{i:03d} -> {p}" for i, p in enumerate(_files(_arts_cache)))
            + "\n\nEmit the findings ledger JSON."
        ),
        is_final=True,
    ))
    return specs


def grade(outputs: dict[str, str], key: dict[str, object], artifacts: dict[str, str]) -> dict[str, object]:
    """F1 of the emitted ledger against the planted truth. Deterministic, no LLM."""
    truth = {f"{f['file']}|{f['kind']}|{f['line']}" for f in key.get("_findings", [])}
    raw = (outputs.get("ledger", "") or "").strip().strip("`")
    raw = raw.removeprefix("json").strip()
    try:
        got = json.loads(raw)
        emitted = {
            f"{f.get('file')}|{f.get('kind')}|{f.get('line')}" for f in got.get("findings", [])
        }
    except (json.JSONDecodeError, AttributeError, TypeError):
        emitted = set()

    tp = len(truth & emitted)
    precision = tp / len(emitted) if emitted else 0.0
    recall = tp / len(truth) if truth else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    # Per-file extraction accuracy is reported as a diagnostic: it isolates whether a failure is in
    # the expensive parallel layer or in the aggregation above it.
    files = _files(artifacts)
    per_item: dict[str, bool] = {}
    for i, path in enumerate(files):
        want = _facts(artifacts[path])
        rawf = (outputs.get(f"facts{i:03d}", "") or "").strip().strip("`").removeprefix("json").strip()
        try:
            gotf = json.loads(rawf)
            per_item[path] = all(
                [int(x) for x in gotf.get(k, [])] == want[k]
                for k in ("sources", "sinks", "sanitizers", "secrets")
            )
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            per_item[path] = False

    extraction_accuracy = (sum(per_item.values()) / len(files)) if files else 0.0
    return {
        "score": f1, "passed": f1 >= 0.90, "n": len(truth), "correct": tp,
        "precision": precision, "recall": recall,
        "extraction_accuracy": extraction_accuracy, "per_item": per_item,
    }


def mutate(text: str, rng) -> tuple[str, str] | None:
    """Family-specific perturbation that is verified to move a finding.

    The generic mutators cannot do this job here. They target numbers, snake_case symbols and
    quoted strings, which on these artifacts means file-header comments and parameter names -
    edits that change the content hash while changing no correct answer. That is a perturbation
    that does not perturb, and it was caught by the semantic gate on the first DAG pilot.

    A first attempt at a family mutator was ALSO largely inert (4 of 12 edits moved a finding):
    promoting a NOTE to a SINK changes nothing when no SOURCE sits above it, and removing a
    SOURCE changes nothing when no SINK sits below it. So the edit is no longer assumed to be
    semantic - it is checked, here, against the generator's own truth function, and only an edit
    that demonstrably changes the finding set is returned.
    """
    before = _findings_for("x", text)
    lines = text.splitlines()
    candidates = [i for i, l in enumerate(lines) if not l.startswith("#")]
    if not candidates:
        return None

    edits: list[tuple[list[str], str]] = []
    for i in candidates:
        line = lines[i]
        n = rng.randrange(100, 999)
        if line.startswith("SANITIZE"):
            edits.append((lines[:i] + [f"NOTE removed guard {n}"] + lines[i + 1:],
                          f"SANITIZE -> NOTE at line {i + 1}"))
        elif line.startswith("NOTE"):
            edits.append((lines[:i] + [f"SINK db_execute_{n}"] + lines[i + 1:],
                          f"NOTE -> SINK at line {i + 1}"))
            edits.append((lines[:i] + [f"SOURCE request_param_{n}"] + lines[i + 1:],
                          f"NOTE -> SOURCE at line {i + 1}"))
            edits.append((lines[:i] + [f'SECRET api_key = "sk_{n:05d}"'] + lines[i + 1:],
                          f"NOTE -> SECRET at line {i + 1}"))
        elif line.startswith("SINK"):
            edits.append((lines[:i] + [f"SANITIZE escape_{n}"] + lines[i:],
                          f"SANITIZE inserted above sink at line {i + 1}"))
            edits.append((lines[:i] + [f"NOTE retired sink {n}"] + lines[i + 1:],
                          f"SINK -> NOTE at line {i + 1}"))
        elif line.startswith("SECRET"):
            edits.append((lines[:i] + [f"NOTE rotated credential {n}"] + lines[i + 1:],
                          f"SECRET removed at line {i + 1}"))
        elif line.startswith("SOURCE"):
            edits.append((lines[:i] + [f"NOTE trusted input {n}"] + lines[i + 1:],
                          f"SOURCE -> NOTE at line {i + 1}"))
            edits.append((lines + [f"SINK db_execute_{n}"],
                          f"SINK appended below existing source (line {len(lines) + 1})"))

    rng.shuffle(edits)
    for new_lines, detail in edits:
        candidate = "\n".join(new_lines) + "\n"
        if _findings_for("x", candidate) != before:
            return candidate, detail + " [verified to change the finding set]"
    return None
