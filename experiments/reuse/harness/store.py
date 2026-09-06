"""The cross-run reuse store.

On-disk and keyed only by content, so it genuinely spans runs, processes, sessions, days and
users. That is not a detail: if the store is scoped to a single session, the thing being
measured is a much weaker system than the one proposed (false-negative item 7). Every hit
therefore records the run that wrote the entry and the run that read it, and
`cross_run_evidence` reports how many hits actually crossed a run boundary. A store that only
ever hits within its own writing run would make the whole arm C result void.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Hit:
    reuse_key: str
    written_by_run: str
    written_at: float
    read_by_run: str
    cross_run: bool
    cost_if_recomputed: float


class ReuseStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS entries (
                   reuse_key TEXT PRIMARY KEY,
                   op_type   TEXT NOT NULL,
                   kind      TEXT NOT NULL,
                   output    TEXT NOT NULL,
                   output_hash TEXT NOT NULL,
                   tokens    TEXT NOT NULL,
                   run_id    TEXT NOT NULL,
                   family    TEXT NOT NULL,
                   written_at REAL NOT NULL,
                   bytes     INTEGER NOT NULL
               )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS hits (
                   reuse_key TEXT NOT NULL,
                   read_by_run TEXT NOT NULL,
                   written_by_run TEXT NOT NULL,
                   cross_run INTEGER NOT NULL,
                   read_at REAL NOT NULL
               )"""
        )
        self.conn.commit()

    # -- core ----------------------------------------------------------------------

    def put(
        self,
        *,
        reuse_key: str,
        op_type: str,
        kind: str,
        output: str,
        output_hash: str,
        tokens: dict[str, int],
        run_id: str,
        family: str,
        now: float | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                reuse_key,
                op_type,
                kind,
                output,
                output_hash,
                json.dumps(tokens, sort_keys=True),
                run_id,
                family,
                now if now is not None else time.time(),
                len(output.encode("utf-8")),
            ),
        )
        self.conn.commit()

    def get(self, reuse_key: str, *, read_by_run: str, now: float | None = None) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT output, output_hash, tokens, run_id, written_at, kind, op_type"
            " FROM entries WHERE reuse_key=?",
            (reuse_key,),
        ).fetchone()
        if row is None:
            return None
        output, output_hash, tokens, writer, written_at, kind, op_type = row
        cross_run = writer != read_by_run
        self.conn.execute(
            "INSERT INTO hits VALUES (?,?,?,?,?)",
            (
                reuse_key,
                read_by_run,
                writer,
                1 if cross_run else 0,
                now if now is not None else time.time(),
            ),
        )
        self.conn.commit()
        return {
            "output": output,
            "output_hash": output_hash,
            "tokens": json.loads(tokens),
            "written_by_run": writer,
            "written_at": written_at,
            "cross_run": cross_run,
            "kind": kind,
            "op_type": op_type,
        }

    # -- evidence and costs ---------------------------------------------------------

    def cross_run_evidence(self) -> dict[str, Any]:
        total, cross = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cross_run),0) FROM hits"
        ).fetchone()
        distinct_writers = self.conn.execute(
            "SELECT COUNT(DISTINCT written_by_run) FROM hits WHERE cross_run=1"
        ).fetchone()[0]
        span = self.conn.execute(
            "SELECT COALESCE(MAX(h.read_at - e.written_at), 0) FROM hits h"
            " JOIN entries e ON e.reuse_key = h.reuse_key WHERE h.cross_run=1"
        ).fetchone()[0]
        return {
            "hits_total": total,
            "hits_cross_run": cross,
            "cross_run_fraction": (cross / total) if total else 0.0,
            "distinct_writing_runs_hit": distinct_writers,
            "max_write_to_read_seconds": span,
        }

    def storage_bytes(self) -> int:
        """Total stored payload. Feeds the storage cost line, because a persistent store has an
        ongoing cost and omitting it flatters arm C (false-positive item 10)."""
        return self.conn.execute("SELECT COALESCE(SUM(bytes),0) FROM entries").fetchone()[0]

    def entry_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def close(self) -> None:
        self.conn.close()
