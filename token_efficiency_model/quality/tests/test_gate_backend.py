"""Judge-backend egress tests: the LLM judge must FAIL CLOSED.

Running the judge ships customer-derived text (their question and both answers) to a
third-party API, so it is an explicit-opt-in subprocessor decision — BREVITAS_JUDGE_BACKEND
must name the backend. These tests pin the three ways the old preference-list loader could
egress by accident:

  * no opt-in at all            -> empty triple, .env.local never even opened;
  * opt-in named, key missing   -> empty triple, no crash, no substitute provider;
  * opt-in names provider X but only provider Y's key exists -> Y's key is NEVER returned.

Plus the consequence that makes fail-closed safe: with no judge, assess() degrades to the
local in-process embedding path and stays degraded=True / passed=False (unverified ⇒
unbilled). Deterministic — no live API calls, no network.
"""
from __future__ import annotations

import pytest

from token_efficiency_model.quality import gate
from token_efficiency_model.quality.gate import QualityGate, QualityGateConfig


# --------------------------------------------------------------------------- #
# isolation
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated_judge_env(monkeypatch, tmp_path):
    """No ambient credential and no real .env.local may reach these tests. This repo's own
    .env.local carries BOTH DEEPSEEK_API_KEY and OPENAI_API_KEY — that is precisely the
    hazard under test, so a test that accidentally read it would pass for the wrong
    reason."""
    for var in ("BREVITAS_JUDGE_BACKEND", "Deepseek_api_key", "DEEPSEEK_API_KEY",
                "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gate, "_ENV_FILE", tmp_path / "nonexistent.env.local")
    return tmp_path


def _env_local(monkeypatch, tmp_path, text: str):
    """Point the module's .env.local at a fixture file with `text` in it."""
    p = tmp_path / ".env.local"
    p.write_text(text)
    monkeypatch.setattr(gate, "_ENV_FILE", p)
    return p


class _Tripwire:
    """Stands in for .env.local and records any access. Never yields a credential."""

    name = ".env.local"

    def __init__(self):
        self.touched = False

    def exists(self):
        self.touched = True
        return False

    def read_text(self):
        self.touched = True
        return ""


# --------------------------------------------------------------------------- #
# 1. unset ⇒ no judge, and no .env.local read at all
# --------------------------------------------------------------------------- #
def test_unset_backend_returns_empty_triple():
    assert gate._load_key() == ("", "", "")


def test_unset_backend_never_opens_env_local(monkeypatch, tmp_path):
    """The sharpest edge: an unrelated local credential must not become egress. With no
    opt-in the file is not merely ignored — it is never opened."""
    _env_local(monkeypatch, tmp_path, "Deepseek_api_key=sk-from-disk\nOPENAI_API_KEY=sk-oai\n")
    assert gate._load_key() == ("", "", "")          # contents cannot leak in

    tw = _Tripwire()
    monkeypatch.setattr(gate, "_ENV_FILE", tw)
    assert gate._load_key() == ("", "", "")
    assert not tw.touched, "no opt-in, yet .env.local was read"


def test_unset_backend_ignores_ambient_provider_keys(monkeypatch):
    """Merely having a DeepSeek (or OpenAI) key exported must not enable the judge."""
    monkeypatch.setenv("Deepseek_api_key", "sk-deepseek-ambient")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-ambient2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-ambient")
    assert gate._load_key() == ("", "", "")


def test_blank_and_unknown_backend_fail_closed(monkeypatch):
    """A blank value or a typo must deny, not fall through to some other subprocessor."""
    monkeypatch.setenv("Deepseek_api_key", "sk-deepseek")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    for value in ("", "   ", "deepsek", "anthropic", "cheapest", "true"):
        monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", value)
        assert gate._load_key() == ("", "", ""), f"{value!r} did not fail closed"


# --------------------------------------------------------------------------- #
# 2. opted in but unusable ⇒ empty triple, no crash, no substitution
# --------------------------------------------------------------------------- #
def test_deepseek_opt_in_without_key_returns_empty_triple(monkeypatch):
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "deepseek")
    assert gate._load_key() == ("", "", "")          # no crash, just no judge


def test_openai_opt_in_never_returns_a_deepseek_credential(monkeypatch, tmp_path):
    """Named provider X, only provider Y's key on the box ⇒ deny. Borrowing Y's key would
    egress to a subprocessor the operator did not name."""
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "openai")
    monkeypatch.setenv("Deepseek_api_key", "sk-deepseek-lowercase")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-upper")
    _env_local(monkeypatch, tmp_path, "DEEPSEEK_API_KEY=sk-deepseek-on-disk\n")

    key, base, model = gate._load_key()
    assert (key, base, model) == ("", "", "")
    assert "deepseek" not in f"{key}{base}{model}".lower()


def test_deepseek_opt_in_never_returns_an_openai_credential(monkeypatch, tmp_path):
    """The mirror case — the fallthrough used to run in this direction."""
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "deepseek")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-only")
    _env_local(monkeypatch, tmp_path, "OPENAI_API_KEY=sk-openai-on-disk\n")
    assert gate._load_key() == ("", "", "")


def test_unreadable_env_local_fails_closed_without_raising(monkeypatch, tmp_path):
    """A malformed/unreadable file must not raise out of QualityGate.__init__."""
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "deepseek")

    class _Boom:
        name = ".env.local"

        def exists(self):
            return True

        def read_text(self):
            raise OSError("permission denied")

    monkeypatch.setattr(gate, "_ENV_FILE", _Boom())
    assert gate._load_key() == ("", "", "")
    assert QualityGate(QualityGateConfig()).judge_key == ""


# --------------------------------------------------------------------------- #
# 3. opted in and usable ⇒ exactly the named backend (positive controls)
# --------------------------------------------------------------------------- #
def test_deepseek_opt_in_with_env_key(monkeypatch):
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "  DeepSeek  ")   # normalized
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    assert gate._load_key() == ("sk-ds", "https://api.deepseek.com/v1", "deepseek-chat")


def test_lowercase_key_name_still_wins_precedence(monkeypatch):
    """Preserves the historical order: Deepseek_api_key before DEEPSEEK_API_KEY."""
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "deepseek")
    monkeypatch.setenv("Deepseek_api_key", "sk-lower")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-upper")
    assert gate._load_key()[0] == "sk-lower"


def test_openai_opt_in_with_env_key(monkeypatch):
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    assert gate._load_key() == ("sk-oai", "https://api.openai.com/v1", "gpt-4o-mini")


def test_env_local_is_read_only_once_opted_in(monkeypatch, tmp_path):
    """The file read is gated, not removed: after an explicit opt-in it still supplies the
    named backend's own credential — and only that one."""
    _env_local(monkeypatch, tmp_path,
               "# comment\n\nOPENAI_API_KEY=sk-oai-disk\nDEEPSEEK_API_KEY=sk-ds-disk\n")
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "deepseek")
    assert gate._load_key() == ("sk-ds-disk", "https://api.deepseek.com/v1", "deepseek-chat")


def test_process_env_beats_env_local(monkeypatch, tmp_path):
    _env_local(monkeypatch, tmp_path, "DEEPSEEK_API_KEY=sk-ds-disk\n")
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-env")
    assert gate._load_key()[0] == "sk-ds-env"


def test_explicit_backend_argument_overrides_the_env_var(monkeypatch):
    monkeypatch.setenv("BREVITAS_JUDGE_BACKEND", "deepseek")
    monkeypatch.setenv("Deepseek_api_key", "sk-ds")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    assert gate._load_key("openai")[0] == "sk-oai"
    assert gate._load_key("") == ("", "", "")


# --------------------------------------------------------------------------- #
# 4. "no judge" degrades to the local embedding path — never crashes, never passes
# --------------------------------------------------------------------------- #
def test_no_opt_in_degrades_to_local_embedding_without_calling_out(monkeypatch):
    g = QualityGate(QualityGateConfig(floor=0.8))
    assert (g.judge_key, g.judge_base, g.judge_model) == ("", "", "")

    emb_calls, judge_calls = [], []
    monkeypatch.setattr(g, "_embedding_similarity",
                        lambda a, b: emb_calls.append((a, b)) or 0.99)
    monkeypatch.setattr(g, "_judge_once",
                        lambda *a, **k: judge_calls.append(a) or (1.0, "ok"))

    res = g.assess("opt", "ref", "q?")
    assert judge_calls == [], "judge ran without an explicit backend opt-in"
    assert emb_calls, "local embedding fallback never ran"
    assert res.degraded and not res.passed and res.judge_score is None
    assert res.embedding_similarity == 0.99
    # reported, not silently zeroed — and never inflated above the local signal
    assert 0.0 < res.score <= res.embedding_similarity
    assert res.fallback_reason and "BREVITAS_JUDGE_BACKEND" in res.fallback_reason


def test_degraded_cannot_pass_even_at_a_zero_floor(monkeypatch):
    """Unverified ⇒ unbilled: degraded is hard-denied, not threshold-denied."""
    g = QualityGate(QualityGateConfig(floor=0.0))
    monkeypatch.setattr(g, "_embedding_similarity", lambda a, b: 1.0)
    res = g.assess("same", "same", "q?")
    assert res.degraded and not res.passed


def test_no_judge_and_no_embedding_model_still_does_not_crash(monkeypatch):
    """Both signals unavailable: score 0.0, degraded, no exception escapes."""
    g = QualityGate(QualityGateConfig())
    monkeypatch.setattr(g, "_encoder", lambda: None)      # sentence_transformers missing
    res = g.assess("opt", "ref", "q?")
    assert res.embedding_similarity == 0.0
    assert res.score == 0.0 and res.degraded and not res.passed
