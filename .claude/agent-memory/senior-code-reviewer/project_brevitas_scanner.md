---
name: brevitas-scanner-module
description: Architecture and key findings from reviewing the Brevitas codebase scanner + auto-integrator feature (scanner/, cli.py scan/apply commands)
metadata:
  type: project
---

Scanner sits in `brevitas/scanner/` (providers, models, detector, codemod, report, __init__). Public entry points: `scan_path`, `plan_changes`, `write_changes`. Exposed via CLI as `brevitas scan` and `brevitas apply --write`.

**Why:** Statically finds LLM client constructions (Anthropic/OpenAI) and rewrites them to `brevitas.wrap(...)` so compression sits between agents automatically.

**How to apply:** When reviewing future scanner changes, watch for the known patterns below.

## Verified architecture facts

- `col_offset` in CPython 3.12 AST is a **UTF-8 byte offset** within the line, not a Unicode code point count. The codemod's byte-arithmetic is correct. Empirically confirmed with `résumé = foo()` → `col_offset=11` matches bytes, not chars (9).
- `write_changes` does **non-atomic direct-overwrite writes** (no temp-file + rename). This is the biggest file-safety risk in the module.
- `_SKIP_DIRS` contains `.env` which is a file, not a directory — `os.walk` only prunes `dirnames`, so the entry is dead code.
- `_wrapped_ids` pre-pass only catches inline `brevitas.wrap(Client())` constructions, not `raw = Client(); client = brevitas.wrap(raw)`. The latter causes a false APPLY → double-wrap at runtime.
- `_call_path` fires on any attribute chain ending in `create`/`stream` matching a registered path — including non-LLM objects like Twilio with `.messages.create()`. Produces false CALL_SITE findings with `provider="unknown"`.
- `visit_AnnAssign` is not implemented; annotated assignments miss `_client_vars` tracking (call-site attribution shows "unknown").
- `_import_insert_line` returns 1 for files with no imports and no docstring, inserting `import brevitas` before a shebang line `#!/usr/bin/env python3`.
- `test_ignores_unrelated_classes` uses `assert A or B` — the assertion cannot catch a regression in A because B is always true.
- Tests live in `brevitas/scanner/tests/test_scanner.py` (no `__init__.py` in tests dir). 11 tests, all pass.
