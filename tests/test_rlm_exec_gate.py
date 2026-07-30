"""The RLM REPL is an unsandboxed exec() and must not run unless asked for.

The namespace it exposes is not a sandbox and cannot be made one in-process, so the
only honest control is an explicit opt-in. These tests pin that the shipped default is
off, that the escape really is possible once enabled (so nobody re-labels it "safe"),
and that emitted code cannot take the host process down.
"""
from __future__ import annotations

import warnings

from token_efficiency_model.lossless.rlm import RLM, REPLState


def _rlm(**kwargs) -> RLM:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return RLM(llm=lambda prompt: "", **kwargs)


def test_exec_is_off_by_default(monkeypatch):
    monkeypatch.delenv("BREVITAS_RLM_ALLOW_EXEC", raising=False)
    repl = _rlm()
    assert repl.allow_unsandboxed_exec is False
    out = repl._repl(REPLState(P="doc", question="q"), "print('ran')")
    assert "ran" not in out
    assert "disabled" in out            # reported back like any other REPL error


def test_run_does_not_exec_when_disabled(monkeypatch):
    monkeypatch.delenv("BREVITAS_RLM_ALLOW_EXEC", raising=False)
    executed = []

    def fake_llm(text: str) -> str:
        if "Question:" in text:
            return "```python\nexecuted.append(1)\nset_final('done')\n```"
        return "fallback"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = RLM(fake_llm, max_iters=1).run("small P", "q")
    assert executed == []
    assert result.answer != "done"


def test_explicit_opt_in_runs_code_and_warns(monkeypatch):
    monkeypatch.delenv("BREVITAS_RLM_ALLOW_EXEC", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        repl = RLM(llm=lambda prompt: "", allow_unsandboxed_exec=True)
    assert any("no sandbox" in str(w.message) for w in caught)
    out = repl._repl(REPLState(P="doc", question="who?"), "print(question)")
    assert "who?" in out


def test_env_flag_opts_in(monkeypatch):
    monkeypatch.setenv("BREVITAS_RLM_ALLOW_EXEC", "1")
    repl = _rlm()
    assert repl.allow_unsandboxed_exec is True


def test_the_namespace_is_not_a_sandbox_when_enabled():
    """Documented, not defended: enabling exec grants full process access."""
    repl = _rlm(allow_unsandboxed_exec=True)
    out = repl._repl(REPLState(P="doc"), "print(len.__self__.__import__('os').name)")
    assert "<error" not in out
    assert out.strip() != ""


def test_system_exit_in_emitted_code_does_not_kill_the_host():
    repl = _rlm(allow_unsandboxed_exec=True)
    out = repl._repl(REPLState(P="doc"), "raise SystemExit(3)")
    assert "SystemExit" in out
