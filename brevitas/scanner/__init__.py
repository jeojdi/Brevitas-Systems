"""
Brevitas codebase scanner.

Statically locates LLM API calls in a codebase and, where appropriate, rewrites
the client construction to route through Brevitas — so compression sits between
your agents with no manual wiring.

Public API:
    scan_path(root)        -> ScanReport
    plan_changes(report)   -> list[FileChange]   (dry-run codemod)
    write_changes(changes) -> int                (persist to disk)
"""
from __future__ import annotations

from .codemod import FileChange, plan_changes, rewrite_source, write_changes
from .detector import scan_path, scan_source
from .models import Finding, Kind, Recommendation, ScanReport

__all__ = [
    "scan_path",
    "scan_source",
    "plan_changes",
    "rewrite_source",
    "write_changes",
    "ScanReport",
    "Finding",
    "FileChange",
    "Kind",
    "Recommendation",
]
