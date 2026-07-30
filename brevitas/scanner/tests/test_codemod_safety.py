"""Codemod safety: the emitted code must import, and the write must not lose data.

These files are the customer's own source, so a bad rewrite is a supply-chain bug:
`brevitas.wrap(...)` without a module-scope `import brevitas` is a NameError at import
time, and a write that ignores concurrent edits destroys uncommitted work.
"""
from __future__ import annotations

import os
import stat
import textwrap
import warnings

import pytest

from brevitas.scanner import plan_changes, rewrite_source, scan_path, scan_source, write_changes
from brevitas.scanner.codemod import CodemodError


def _rewrite(src: str):
    src = textwrap.dedent(src)
    return rewrite_source("x.py", src, scan_source("x.py", src))


def _module_level_import_lines(modified: str) -> list[str]:
    # Unindented only: a naive `"import brevitas" in modified` passes vacuously
    # against a function-local import, which is exactly how this bug hid.
    return [ln for ln in modified.splitlines() if ln == "import brevitas"]


# ── the import predicate ──────────────────────────────────────────────────────

def test_from_brevitas_import_wrap_still_gets_the_module_import():
    change = _rewrite("""
        from brevitas import wrap
        import openai
        a = wrap(openai.OpenAI())
        b = openai.OpenAI()
    """)
    assert change is not None
    assert "brevitas.wrap(openai.OpenAI())" in change.modified
    assert _module_level_import_lines(change.modified) == ["import brevitas"]


def test_aliased_import_still_gets_the_module_import():
    change = _rewrite("""
        import brevitas as bv
        import openai
        a = bv.wrap(openai.OpenAI())
        b = openai.OpenAI()
    """)
    assert change is not None
    assert _module_level_import_lines(change.modified) == ["import brevitas"]


def test_function_local_import_does_not_satisfy_module_scope():
    change = _rewrite("""
        import openai

        def build():
            import brevitas
            return brevitas.wrap(openai.OpenAI())

        client = openai.OpenAI()
    """)
    assert change is not None
    assert _module_level_import_lines(change.modified) == ["import brevitas"]


def test_submodule_import_already_binds_the_name():
    change = _rewrite("""
        import brevitas.wrappers
        import openai
        client = openai.OpenAI()
    """)
    assert change is not None
    assert _module_level_import_lines(change.modified) == []      # no duplicate insert
    assert "brevitas.wrap(openai.OpenAI())" in change.modified


def test_type_checking_only_import_is_not_counted():
    change = _rewrite("""
        from typing import TYPE_CHECKING
        import openai
        if TYPE_CHECKING:
            import brevitas
        client = openai.OpenAI()
    """)
    assert change is not None
    assert _module_level_import_lines(change.modified) == ["import brevitas"]


@pytest.mark.parametrize("preamble", [
    "from brevitas import wrap",
    "import brevitas as bv",
])
def test_rewritten_module_actually_imports(tmp_path, preamble):
    """The whole point: the rewritten file must still be importable.

    Run it in a subprocess against stub brevitas/openai modules, so this asserts on
    real interpreter behaviour rather than on the presence of a text line.
    """
    change = _rewrite(f"""
        {preamble}
        import openai
        client = openai.OpenAI()
    """)
    (tmp_path / "app.py").write_text(change.modified)
    (tmp_path / "openai.py").write_text("class OpenAI:\n    def __init__(self, *a, **k):\n        pass\n")
    (tmp_path / "brevitas.py").write_text("def wrap(client, **k):\n    return client\n")

    import subprocess
    import sys
    done = subprocess.run(
        [sys.executable, "-c", "import app; assert app.client is not None"],
        cwd=tmp_path, capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")},
    )
    assert done.returncode == 0, done.stderr        # NameError before the fix


def test_stale_offsets_are_refused_rather_than_spliced():
    src = textwrap.dedent("""
        import openai
        client = openai.OpenAI()
    """)
    findings = scan_source("x.py", src)
    # The file grew a line since the scan: offsets now point mid-expression.
    with pytest.raises((SyntaxError, CodemodError)):
        rewrite_source("x.py", "x = 1\n" + src, findings)


# ── write_changes ─────────────────────────────────────────────────────────────

_SRC = 'import openai\nclient = openai.OpenAI()\n'


def _plan(tmp_path, name="bin/ingest.py", body=_SRC):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    changes = plan_changes(scan_path(str(tmp_path)))
    assert len(changes) == 1
    return target, changes


def test_write_preserves_file_mode(tmp_path):
    target, changes = _plan(tmp_path, body="#!/usr/bin/env python3\n" + _SRC)
    os.chmod(target, 0o755)
    assert write_changes(changes) == 1
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755
    assert "brevitas.wrap(" in target.read_text()


def test_write_through_symlink_keeps_the_link(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    real = shared / "settings.py"
    real.write_text(_SRC)
    link_dir = tmp_path / "service"
    link_dir.mkdir()
    link = link_dir / "settings.py"
    link.symlink_to(os.path.relpath(real, link_dir))

    changes = [c for c in plan_changes(scan_path(str(link_dir))) if c.path.endswith("settings.py")]
    assert len(changes) == 1
    assert write_changes(changes) == 1
    assert link.is_symlink()                         # link, not a divergent copy
    assert "brevitas.wrap(" in real.read_text()


def test_concurrent_edit_is_skipped_not_clobbered(tmp_path):
    target, changes = _plan(tmp_path, name="app.py")
    # The developer edits the file while the confirmation prompt is open.
    edited = _SRC + "MY_UNCOMMITTED_EDIT = 1\n"
    target.write_text(edited)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert write_changes(changes) == 0
    assert target.read_text() == edited              # edit survives
    assert any("changed on disk" in str(w.message) for w in caught)


def test_unchanged_file_still_writes(tmp_path):
    target, changes = _plan(tmp_path, name="app.py")
    assert write_changes(changes) == 1
    assert "brevitas.wrap(" in target.read_text()
