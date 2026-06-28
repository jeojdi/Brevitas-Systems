"""
Static detection of LLM client constructions and call sites.

Everything here is AST-based. We deliberately avoid regexes: resolving import
aliases, distinguishing ``brevitas.wrap(Anthropic())`` from a bare
``Anthropic()``, and capturing exact source spans for a safe codemod all
require real syntax understanding.
"""
from __future__ import annotations

import ast
import os

from . import providers
from .models import Finding, Kind, Recommendation, ScanReport

# Directories we never descend into — vendored code, caches, build output.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", "venv", ".venv", "env",
    "site-packages", "build", "dist", ".tox", ".eggs",
})


class _FileScanner(ast.NodeVisitor):
    """Collects findings from a single parsed module."""

    def __init__(self, path: str, source: str) -> None:
        self.path = path
        self.source = source
        self.findings: list[Finding] = []

        # Import resolution tables, populated by the visitor.
        self._module_aliases: dict[str, str] = {}          # local name -> provider module
        self._class_aliases: dict[str, tuple[str, bool]] = {}  # local name -> (provider, is_async)
        self._brevitas_wrap_names: set[str] = set()        # local names bound to brevitas.wrap

        # node ids of constructions already routed through brevitas.wrap(...).
        self._wrapped_ids: set[int] = set()
        # variable names later passed to brevitas.wrap(var).
        self._wrapped_vars: set[str] = set()
        # variable name -> (provider, is_async) for client assignments.
        self._client_vars: dict[str, tuple[str, bool]] = {}

    # -- entry point -----------------------------------------------------------

    def run(self, tree: ast.AST) -> list[Finding]:
        # Pass 1: resolve imports and mark already-wrapped constructions, so the
        # main pass has full context regardless of statement order.
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._record_import(node)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and self._is_wrap_call(node.func) and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Call):          # brevitas.wrap(Anthropic())
                    self._wrapped_ids.add(id(arg))
                elif isinstance(arg, ast.Name):        # raw = Anthropic(); brevitas.wrap(raw)
                    self._wrapped_vars.add(arg.id)
        # Link an assigned-then-wrapped variable back to its construction node,
        # so the indirect form is reported as DONE rather than double-wrapped.
        for node in ast.walk(tree):
            target = _assign_target(node)
            value = getattr(node, "value", None)
            if (
                isinstance(target, ast.Name)
                and target.id in self._wrapped_vars
                and isinstance(value, ast.Call)
            ):
                self._wrapped_ids.add(id(value))
        # Pass 2: walk for client constructions, then for call sites.
        self.visit(tree)
        return self.findings

    # -- imports ---------------------------------------------------------------

    def _record_import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name in providers.known_modules():
                    self._module_aliases[local] = alias.name
                if alias.name == "brevitas":
                    # `import brevitas` -> wrap is reached as `brevitas.wrap`
                    self._module_aliases.setdefault(local, "brevitas")
            return

        # ImportFrom
        if node.module in providers.known_modules():
            for alias in node.names:
                cls = providers.classify_class(alias.name)
                if cls is not None:
                    self._class_aliases[alias.asname or alias.name] = cls
        elif node.module == "brevitas":
            for alias in node.names:
                if alias.name == "wrap":
                    self._brevitas_wrap_names.add(alias.asname or alias.name)

    # -- wrap detection --------------------------------------------------------

    def _is_wrap_call(self, func: ast.expr) -> bool:
        # `brevitas.wrap(...)`
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "wrap"
            and isinstance(func.value, ast.Name)
            and self._module_aliases.get(func.value.id) == "brevitas"
        ):
            return True
        # `wrap(...)` from `from brevitas import wrap`
        return isinstance(func, ast.Name) and func.id in self._brevitas_wrap_names

    # -- construction classification -------------------------------------------

    def _classify_construction(self, func: ast.expr) -> tuple[str, str, bool] | None:
        """Return (symbol, provider, is_async) if ``func`` builds a known client."""
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = self._module_aliases.get(func.value.id)
            if module is not None:
                hit = providers.classify_module_attr(module, func.attr)
                if hit is not None:
                    return f"{module}.{func.attr}", hit[0], hit[1]
        if isinstance(func, ast.Name):
            hit = self._class_aliases.get(func.id)
            if hit is not None:
                return func.id, hit[0], hit[1]
        return None

    # -- visitors --------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        self._track_client_var(_assign_target(node), node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._track_client_var(node.target, node.value)
        self.generic_visit(node)

    def _track_client_var(self, target: ast.expr | None, value: ast.expr | None) -> None:
        """Record `client = <construction>` (or wrapped construction) so call
        sites can be attributed to a specific client variable."""
        if isinstance(value, ast.Call) and self._is_wrap_call(value.func) and value.args:
            inner = value.args[0]
            value = inner if isinstance(inner, ast.Call) else value
        if isinstance(target, ast.Name) and isinstance(value, ast.Call):
            info = self._classify_construction(value.func)
            if info is not None:
                self._client_vars[target.id] = (info[1], info[2])

    def visit_Call(self, node: ast.Call) -> None:
        info = self._classify_construction(node.func)
        if info is not None:
            self._add_client(node, *info)
        elif (chain := self._call_path(node.func)) is not None:
            self._add_call_site(node, chain)
        self.generic_visit(node)

    # -- finding builders ------------------------------------------------------

    def _add_client(self, node: ast.Call, symbol: str, provider: str, is_async: bool) -> None:
        already = id(node) in self._wrapped_ids
        if already:
            rec, reason = Recommendation.DONE, "already routed through brevitas.wrap()"
        elif is_async:
            rec, reason = Recommendation.MANUAL, "async client — sync wrapper only; wrap manually"
        else:
            rec, reason = Recommendation.APPLY, "unwrapped client — Brevitas can compress its calls"
        self.findings.append(Finding(
            path=self.path,
            line=node.lineno, col=node.col_offset,
            end_line=node.end_lineno or node.lineno,
            end_col=node.end_col_offset or node.col_offset,
            kind=Kind.CLIENT, provider=provider, symbol=symbol,
            source=ast.get_source_segment(self.source, node) or "",
            is_async=is_async, recommendation=rec, reason=reason,
        ))

    def _call_path(self, func: ast.expr) -> tuple[str, ...] | None:
        """If ``func`` is an attribute chain ending in a model call, return the
        chain (e.g. ``("client", "messages", "create")``)."""
        if not isinstance(func, ast.Attribute) or func.attr not in providers.call_tails():
            return None
        parts: list[str] = []
        cur: ast.expr = func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        parts.reverse()
        # Match the tail against any registered call path (e.g. messages.create).
        for spec in providers.REGISTRY:
            for cp in spec.call_paths:
                if tuple(parts[-len(cp):]) == cp:
                    return tuple(parts)
        return None

    def _add_call_site(self, node: ast.Call, chain: tuple[str, ...]) -> None:
        root = chain[0]
        # Only count calls on a variable we've identified as an LLM client, so
        # an unrelated `twilio.messages.create(...)` doesn't masquerade as a hop.
        if root not in self._client_vars:
            return
        provider = self._client_vars[root][0]
        self.findings.append(Finding(
            path=self.path,
            line=node.lineno, col=node.col_offset,
            end_line=node.end_lineno or node.lineno,
            end_col=node.end_col_offset or node.col_offset,
            kind=Kind.CALL_SITE, provider=provider, symbol=".".join(chain),
            source="", var_name=root,
            recommendation=Recommendation.DONE, reason="model call site",
        ))


def _assign_target(node: ast.AST) -> ast.expr | None:
    """The single LHS name of an assignment, if it has exactly one."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return node.targets[0]
    if isinstance(node, ast.AnnAssign):
        return node.target
    return None


def scan_source(path: str, source: str) -> list[Finding]:
    """Scan a single source string. Raises ``SyntaxError`` on unparsable input."""
    tree = ast.parse(source)
    return _FileScanner(path, source).run(tree)


def scan_path(root: str) -> ScanReport:
    """Walk ``root`` (file or directory) and collect findings from all Python files."""
    report = ScanReport()
    for file_path in _iter_python_files(root):
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append((file_path, f"read failed: {exc}"))
            continue
        report.files_scanned += 1
        try:
            report.findings.extend(scan_source(file_path, source))
        except SyntaxError as exc:
            report.errors.append((file_path, f"parse failed: {exc}"))
    return report


def _iter_python_files(root: str):
    if os.path.isfile(root):
        if root.endswith(".py"):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)
