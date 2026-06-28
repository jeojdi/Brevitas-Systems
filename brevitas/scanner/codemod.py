"""
Source rewriting: splice ``brevitas.wrap(...)`` around detected clients.

Edits are computed as byte ranges on the original file and applied from the
end backwards, so earlier offsets stay valid as we go. We work in bytes rather
than characters because CPython's AST ``col_offset`` is a UTF-8 byte offset —
doing this in str-space would corrupt files containing non-ASCII before a wrap
site. A single ``import brevitas`` is injected per file (after ``__future__``).
"""
from __future__ import annotations

import ast
import contextlib
import difflib
import os
import tempfile
from dataclasses import dataclass

from .models import Finding, Recommendation, ScanReport

_IMPORT_LINE = b"import brevitas\n"


@dataclass
class FileChange:
    path: str
    original: str
    modified: str
    wrapped: int  # number of clients wrapped in this file

    @property
    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            self.original.splitlines(keepends=True),
            self.modified.splitlines(keepends=True),
            fromfile=self.path, tofile=self.path,
        ))


def _line_starts(data: bytes) -> list[int]:
    starts = [0]
    for i, byte in enumerate(data):
        if byte == 0x0A:  # \n
            starts.append(i + 1)
    return starts


def _offset(starts: list[int], line: int, col: int) -> int:
    return starts[line - 1] + col


def _has_brevitas_import(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "brevitas" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "brevitas":
            return True
    return False


def _import_insert_line(tree: ast.Module, source: str) -> int:
    """1-indexed line to insert ``import brevitas`` *before*: after the last
    top-level import (so it sits with the import block and after ``__future__``),
    else after a module docstring, else just after a shebang, else the top."""
    last_import_end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import_end = max(last_import_end, node.end_lineno or node.lineno)
    if last_import_end:
        return last_import_end + 1
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        return (tree.body[0].end_lineno or 1) + 1
    # Never insert above a shebang — it must remain the first line.
    if source.startswith("#!"):
        return 2
    return 1


def rewrite_source(path: str, source: str, findings: list[Finding]) -> FileChange | None:
    """Apply wrap edits for one file. Returns ``None`` if nothing changes."""
    targets = [f for f in findings if f.recommendation is Recommendation.APPLY]
    if not targets:
        return None

    data = source.encode("utf-8")
    starts = _line_starts(data)
    tree = ast.parse(source)

    # (start, end, replacement) byte-range edits on the original buffer.
    edits: list[tuple[int, int, bytes]] = []
    for f in targets:
        start = _offset(starts, f.line, f.col)
        end = _offset(starts, f.end_line, f.end_col)
        segment = data[start:end]
        edits.append((start, end, b"brevitas.wrap(" + segment + b")"))

    if not _has_brevitas_import(tree):
        ins_line = _import_insert_line(tree, source)
        ins_off = starts[ins_line - 1] if ins_line - 1 < len(starts) else len(data)
        edits.append((ins_off, ins_off, _IMPORT_LINE))

    # Apply end-to-start so untouched offsets remain valid.
    edits.sort(key=lambda e: e[0], reverse=True)
    buf = bytearray(data)
    for start, end, replacement in edits:
        buf[start:end] = replacement

    modified = buf.decode("utf-8")
    if modified == source:
        return None
    return FileChange(path=path, original=source, modified=modified, wrapped=len(targets))


def plan_changes(report: ScanReport) -> list[FileChange]:
    """Compute file changes for every applicable finding in a scan report."""
    by_file: dict[str, list[Finding]] = {}
    for f in report.applicable:
        by_file.setdefault(f.path, []).append(f)

    changes: list[FileChange] = []
    for path, findings in sorted(by_file.items()):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            continue
        change = rewrite_source(path, source, findings)
        if change is not None:
            changes.append(change)
    return changes


def write_changes(changes: list[FileChange]) -> int:
    """Persist changes to disk atomically. Returns the number of files written.

    Each file is written to a temp file on the same filesystem and then renamed
    over the original via ``os.replace`` — so a crash or error mid-write can
    never leave a user's source truncated or half-written.
    """
    written = 0
    for change in changes:
        target = os.path.abspath(change.path)
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=os.path.dirname(target),
                prefix=".brevitas-", suffix=".tmp", delete=False,
            ) as tmp:
                tmp.write(change.modified)
                tmp_path = tmp.name
            os.replace(tmp_path, target)
            written += 1
        except OSError:
            if tmp_path:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            raise
    return written
