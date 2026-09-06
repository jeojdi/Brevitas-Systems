"""Blinded, resumable ledger.

Blinding is the single highest-value honesty control in the brief, because it removes the
ability to nudge an analysis toward a preferred arm. Arms are written under opaque labels; the
label -> arm mapping lives in a separate file that the analysis script never opens. The analysis
computes every metric over labels, and only after all numbers are final is the mapping applied.

Resumability matters for a different reason: the matrix spans many hours and will be
interrupted. It also creates the temptation the brief warns about - peeking at partial results
and stopping when the number looks conclusive. `pending_cells` therefore always returns the full
pre-committed matrix minus what is done, and never reorders it by how interesting a cell looks.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Iterable

ARMS = ("A", "B", "C")


def blind_label(arm: str, salt: str) -> str:
    return "arm_" + hashlib.sha256(f"{salt}|{arm}".encode("utf-8")).hexdigest()[:4]


class ArmBlinding:
    """Owns the mapping. Written once; read only at unblinding time."""

    def __init__(self, mapping_path: str) -> None:
        self.mapping_path = mapping_path
        if os.path.exists(mapping_path):
            with open(mapping_path, encoding="utf-8") as handle:
                self._blob = json.load(handle)
        else:
            salt = hashlib.sha256(os.urandom(32)).hexdigest()
            labels = {arm: blind_label(arm, salt) for arm in ARMS}
            if len(set(labels.values())) != len(ARMS):
                raise RuntimeError("blinded label collision; regenerate")
            self._blob = {
                "salt": salt,
                "arm_to_label": labels,
                "label_to_arm": {v: k for k, v in labels.items()},
                "created_at": time.time(),
                "unblinded_at": None,
                "note": "The analysis script must not read this file. Unblind only after all "
                        "numbers in results.md are final.",
            }
            with open(mapping_path, "w", encoding="utf-8") as handle:
                json.dump(self._blob, handle, indent=2, sort_keys=True)

    def label(self, arm: str) -> str:
        return self._blob["arm_to_label"][arm]

    def unblind(self) -> dict[str, str]:
        self._blob["unblinded_at"] = time.time()
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump(self._blob, handle, indent=2, sort_keys=True)
        return dict(self._blob["label_to_arm"])


def cell_key(
    family: str, perturbation: float, label: str, temperature: str, seed: int,
    run_size: int = 0,
) -> str:
    """Run size is part of the identity. Omitting it made a size sweep silently no-op: sizes 24 and
    48 were treated as already complete because size 6 had written the same (family, perturbation,
    label, temperature, seed) key."""
    return f"{family}|{perturbation}|{label}|{temperature}|{seed}|{run_size}"


class Ledger:
    def __init__(self, path: str) -> None:
        self.path = path
        self._done: set[str] = set()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn final line from an interrupted run
                    self._done.add(
                        cell_key(
                            record["family"],
                            record["perturbation"],
                            record["arm_label"],
                            record["temperature"],
                            record["seed"],
                            record.get("run_size", 0),
                        )
                    )

    def is_done(self, family: str, perturbation: float, label: str, temperature: str,
                seed: int, run_size: int = 0) -> bool:
        return cell_key(family, perturbation, label, temperature, seed, run_size) in self._done

    def append(self, record: dict[str, Any]) -> None:
        required = ("family", "perturbation", "arm_label", "temperature", "seed")
        missing = [k for k in required if k not in record]
        if missing:
            raise KeyError(f"ledger record missing {missing}")
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._done.add(
            cell_key(
                record["family"],
                record["perturbation"],
                record["arm_label"],
                record["temperature"],
                record["seed"],
                record.get("run_size", 0),
            )
        )

    def pending_cells(self, matrix: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """The pre-committed matrix minus completed cells, in the order the matrix declares it.

        Deliberately not sorted by anything result-dependent. Optional stopping is the failure
        mode here: stopping when a partial number looks conclusive invalidates the result, so
        the only permitted early stop is at a declared checkpoint that covers all arms and
        families equally.
        """
        return [
            cell
            for cell in matrix
            if not self.is_done(
                cell["family"], cell["perturbation"], cell["arm_label"], cell["temperature"],
                cell["seed"], cell.get("run_size", 0),
            )
        ]
