"""Seeded, position-uniform, semantically real perturbation.

Between the first run and the repeat run, a subset of input artifacts is modified. This is what
makes the experiment a test of partial invalidation rather than of running identical work twice,
so two properties have to hold or the whole result is meaningless.

1. The change must be semantically real. A perturbation that alters no correct answer means
   invalidation was never tested (false-positive item 3). Every mutator here changes a value, a
   symbol name or a number that propagates; none of them touch whitespace or comments. The
   family's grader key is required to change, and `verify_answers_changed` enforces it.

2. The sampler must be uniform over graph positions. A change at the root of a chain and a
   change at a leaf have wildly different consequences, so a sampler that happens to favour
   either moves the reuse rate for a reason that has nothing to do with the idea. This is
   false-positive item 8 and false-negative item 5 - the same defect pointing both ways. Here
   uniformity is by construction (stratified over depth), and the achieved distribution is
   reported so it can be checked rather than trusted.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, asdict
from typing import Any, Callable


@dataclass
class Perturbation:
    artifact: str
    mutator: str
    detail: str
    depth: int
    blast_count: int
    blast_fraction: float
    stratum: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------------------
# Mutators. Each returns (new_text, human description) or None if it does not apply.
# ---------------------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])(\d+)(?![\w.])")
_IDENT = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_QUOTED = re.compile(r"\"([A-Za-z][A-Za-z0-9 ._-]{2,40})\"")


def mutate_number(text: str, rng: random.Random) -> tuple[str, str] | None:
    """Change a numeric literal. Numbers propagate through computed answers."""
    hits = list(_NUMBER.finditer(text))
    if not hits:
        return None
    hit = rng.choice(hits)
    old = int(hit.group(1))
    new = old + rng.choice([1, 2, 3, 7, 11, 17])
    if new == old:
        new = old + 1
    return (
        text[: hit.start(1)] + str(new) + text[hit.end(1) :],
        f"number {old} -> {new} at offset {hit.start(1)}",
    )


def mutate_symbol(text: str, rng: random.Random) -> tuple[str, str] | None:
    """Rename a snake_case symbol everywhere it appears. Renames propagate to every reader."""
    hits = sorted({m.group(1) for m in _IDENT.finditer(text)})
    if not hits:
        return None
    old = rng.choice(hits)
    new = f"{old}_v2"
    return re.sub(rf"\b{re.escape(old)}\b", new, text), f"symbol {old} -> {new}"


def mutate_string_value(text: str, rng: random.Random) -> tuple[str, str] | None:
    """Change a quoted value. Extraction and classification answers key off these."""
    hits = list(_QUOTED.finditer(text))
    if not hits:
        return None
    hit = rng.choice(hits)
    old = hit.group(1)
    words = old.split()
    words[rng.randrange(len(words))] = rng.choice(
        ["Meridian", "Kestrel", "Alderton", "Vireo", "Harrowgate", "Sable"]
    )
    new = " ".join(words)
    if new == old:
        new = old + " Ltd"
    return (
        text[: hit.start(1)] + new + text[hit.end(1) :],
        f"value {old!r} -> {new!r} at offset {hit.start(1)}",
    )


MUTATORS: dict[str, Callable[[str, random.Random], tuple[str, str] | None]] = {
    "number": mutate_number,
    "symbol": mutate_symbol,
    "string_value": mutate_string_value,
}


# ---------------------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------------------

N_STRATA = 4


def _stratify(positions: dict[str, dict[str, Any]]) -> dict[int, list[str]]:
    """Split artifacts into equal-population strata ordered by graph depth.

    Stratifying on depth rather than sampling uniformly at random is what makes position
    uniformity a property of the design instead of a property of a particular seed. With a
    2% perturbation of a few hundred artifacts, plain uniform sampling draws so few artifacts
    that whether they land at roots or leaves is essentially luck, and the reuse rate would
    swing on that luck.
    """
    ordered = sorted(positions.items(), key=lambda kv: (kv[1]["depth"], kv[0]))
    strata: dict[int, list[str]] = {i: [] for i in range(N_STRATA)}
    if not ordered:
        return strata
    per = max(1, len(ordered) // N_STRATA)
    for index, (art, _info) in enumerate(ordered):
        strata[min(index // per, N_STRATA - 1)].append(art)
    return strata


def seed_offset(seed: int) -> int:
    """Which stratum a small draw starts from. Rotating by seed is what makes three seeds sample
    three different graph positions rather than the same one three times."""
    return seed % N_STRATA


def select(
    positions: dict[str, dict[str, Any]],
    fraction: float,
    seed: int,
) -> list[tuple[str, int]]:
    """Choose which artifacts to perturb. Returns (artifact, stratum) pairs.

    Deterministic in `seed`: the same seed selects byte-identically the same artifacts.
    """
    if fraction <= 0:
        return []
    rng = random.Random(f"perturb-select-{seed}-{fraction}")
    strata = _stratify(positions)
    target = max(1, round(len(positions) * fraction))
    per_stratum = target / N_STRATA

    chosen: list[tuple[str, int]] = []

    # When the target is smaller than the stratum count - which is the normal case at 2% of a
    # small corpus - the carry-based allocation below can only ever reach the LAST stratum:
    # takes are 0, 0, 0, 1. Three of four graph-position bands become unreachable, every seed
    # draws from the deepest quarter, and the "3 seeds" are one draw replicated. Both adversarial
    # reviewers found this. Rotating the starting stratum by seed makes small draws cover the
    # position space across seeds instead of pinning them to the leaves.
    if target < N_STRATA:
        order = [(seed_offset(seed) + i) % N_STRATA for i in range(N_STRATA)]
        for stratum in order[:target]:
            pool = sorted(set(strata[stratum]) - {a for a, _ in chosen})
            if pool:
                chosen.append((rng.choice(pool), stratum))
        if len(chosen) == target:
            return sorted(chosen)

    carry = 0.0
    for index in range(N_STRATA):
        want = per_stratum + carry
        take = int(want)
        carry = want - take
        pool = sorted(strata[index])
        take = min(take, len(pool))
        for art in rng.sample(pool, take):
            chosen.append((art, index))

    # Distribute any shortfall from rounding across strata that still have capacity, in
    # stratum order, so the deficit does not silently concentrate at one end of the graph.
    index = 0
    while len(chosen) < target and index < N_STRATA * 4:
        stratum = index % N_STRATA
        remaining = sorted(set(strata[stratum]) - {a for a, _ in chosen})
        if remaining:
            chosen.append((rng.choice(remaining), stratum))
        index += 1
    return sorted(chosen)


def apply(
    artifacts: dict[str, str],
    positions: dict[str, dict[str, Any]],
    fraction: float,
    seed: int,
    family_mutator: Callable[[str, random.Random], tuple[str, str] | None] | None = None,
) -> tuple[dict[str, str], list[Perturbation]]:
    """Perturb `fraction` of artifacts. Returns the new artifact map and the record of what
    changed, including each artifact's graph position."""
    selected = select(positions, fraction, seed)
    out = dict(artifacts)
    records: list[Perturbation] = []
    rng = random.Random(f"perturb-apply-{seed}-{fraction}")

    for artifact, stratum in selected:
        text = artifacts.get(artifact)
        if text is None:
            continue

        # A family may supply its own mutator when the generic ones cannot reach the semantics.
        # On the layered-DAG family the generic mutators hit file-header comments and parameter
        # names, which change the artifact hash while changing no correct answer - a perturbation
        # that does not perturb. The family knows which edits actually move a finding.
        if family_mutator is not None:
            result = family_mutator(text, rng)
            if result is not None and result[0] != text:
                out[artifact] = result[0]
                info = positions.get(artifact, {})
                records.append(Perturbation(
                    artifact=artifact, mutator="family", detail=result[1],
                    depth=int(info.get("depth", -1)),
                    blast_count=int(info.get("blast_count", 0)),
                    blast_fraction=float(info.get("blast_fraction", 0.0)),
                    stratum=stratum,
                ))
                continue

        order = sorted(MUTATORS)
        rng.shuffle(order)
        for name in order:
            result = MUTATORS[name](text, rng)
            if result is None or result[0] == text:
                continue
            out[artifact] = result[0]
            info = positions.get(artifact, {})
            records.append(
                Perturbation(
                    artifact=artifact,
                    mutator=name,
                    detail=result[1],
                    depth=int(info.get("depth", -1)),
                    blast_count=int(info.get("blast_count", 0)),
                    blast_fraction=float(info.get("blast_fraction", 0.0)),
                    stratum=stratum,
                )
            )
            break
    return out, records


def position_report(records: list[Perturbation], positions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Achieved position distribution of the perturbation, against the population it was drawn
    from. Reported whether or not it looks good; a skew here invalidates the cell."""
    strata = _stratify(positions)
    pop = {i: len(strata[i]) for i in range(N_STRATA)}
    got: dict[int, int] = {i: 0 for i in range(N_STRATA)}
    for record in records:
        got[record.stratum] = got.get(record.stratum, 0) + 1

    if not records:
        # With nothing perturbed, position uniformity is undefined, not bad. Reporting a large
        # deviation here would spuriously fail a blocking gate on every 0%-perturbation cell.
        return {
            "n_perturbed": 0,
            "by_stratum": {},
            "max_share_deviation": 0.0,
            "undefined_no_perturbations": True,
            "mean_depth": 0.0,
            "mean_blast_fraction": 0.0,
            "population_mean_blast_fraction": (
                sum(p["blast_fraction"] for p in positions.values()) / len(positions)
            ) if positions else 0.0,
        }

    total = sum(got.values()) or 1
    pop_total = sum(pop.values()) or 1
    return {
        "n_perturbed": len(records),
        "by_stratum": {
            str(i): {
                "selected": got.get(i, 0),
                "selected_share": got.get(i, 0) / total,
                "population_share": pop.get(i, 0) / pop_total,
            }
            for i in range(N_STRATA)
        },
        "max_share_deviation": max(
            abs(got.get(i, 0) / total - pop.get(i, 0) / pop_total) for i in range(N_STRATA)
        ),
        "mean_depth": (sum(r.depth for r in records) / len(records)) if records else 0.0,
        "mean_blast_fraction": (
            sum(r.blast_fraction for r in records) / len(records)
        )
        if records
        else 0.0,
        "population_mean_blast_fraction": (
            sum(p["blast_fraction"] for p in positions.values()) / len(positions)
        )
        if positions
        else 0.0,
    }


def verify_answers_changed(
    before_key: dict[str, Any],
    after_key: dict[str, Any],
    records: list[Perturbation],
    answer_key_of: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    artifacts: dict[str, str] | None = None,
    perturbed_artifacts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A perturbation that leaves every correct answer intact did not test invalidation.

    Checked PER PERTURBED ARTIFACT, not in aggregate. Comparing the total count of changed answer
    keys against the number of perturbations is too weak: on a chain, one mutation at the root
    changes every downstream answer, so the aggregate count passes even when the other mutations
    were semantically inert. That is false-positive item 3 slipping through its own gate, and it
    happened in the pilot - two mutations landed on a module's identifier rather than its constant
    and were counted as semantic because a third mutation had moved nine keys.

    When the family's `answer_key` function and both artifact maps are supplied, each perturbation
    is isolated: apply only that one artifact's change and confirm at least one answer moves.
    """
    changed = [k for k in after_key if before_key.get(k) != after_key.get(k)]
    per_artifact: dict[str, bool] = {}

    if answer_key_of is not None and artifacts is not None and perturbed_artifacts is not None:
        for record in records:
            isolated = dict(artifacts)
            isolated[record.artifact] = perturbed_artifacts[record.artifact]
            try:
                moved = answer_key_of(isolated) != before_key
            except Exception:
                moved = False  # a perturbation that breaks parsing is not a valid perturbation
            per_artifact[record.artifact] = moved
        all_semantic = all(per_artifact.values()) if per_artifact else True
    else:
        all_semantic = len(changed) >= len(records) if records else True

    return {
        "n_perturbed_artifacts": len(records),
        "n_answers_changed": len(changed),
        "answers_changed": sorted(changed)[:64],
        "per_artifact_semantic": per_artifact,
        "n_inert_perturbations": sum(1 for v in per_artifact.values() if not v),
        "all_perturbations_semantic": all_semantic,
    }
