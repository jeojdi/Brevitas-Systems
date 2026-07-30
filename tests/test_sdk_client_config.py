"""SDK-side configuration and CLI disclosure invariants.

The Brevitas API key travels in a header on every metered call, so the shipped
default base URL must be https and must be a bare origin; the CLI must not claim to
have persisted a secret it did not, and must not ship source anywhere without asking.
"""
from __future__ import annotations

import json
import re
import warnings

import pytest
from click.testing import CliRunner

import brevitas.config as config_module
from brevitas.cli import main

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """rich colours the CLI; assert on the words, not the escape codes."""
    return _ANSI.sub("", text)


# ── base_url default + validator (finding: cleartext key to localhost:8000) ────

def test_shipped_default_base_url_is_https_bare_origin(monkeypatch):
    monkeypatch.delenv("BREVITAS_BASE_URL", raising=False)
    monkeypatch.delenv("BREVITAS_API_KEY", raising=False)
    import importlib
    fresh = importlib.reload(config_module)
    try:
        base_url = fresh.get()["base_url"]
        assert base_url.startswith("https://")
        # report_usage joins f"{base_url}/v1/usage" — a /v1 suffix would give /v1/v1.
        assert not base_url.rstrip("/").endswith("/v1")
        assert "localhost" not in base_url and "127.0.0.1" not in base_url
    finally:
        importlib.reload(config_module)


def _warnings_for(url: str) -> list[str]:
    config_module._warned_base_urls.discard(url)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config_module.check_base_url(url)
    return [str(w.message) for w in caught]


def test_validator_warns_on_cleartext_and_on_v1_suffix():
    assert any("cleartext" in m for m in _warnings_for("http://api.example.com"))
    assert any("/v1/v1" in m for m in _warnings_for("https://brevitassystems.com/v1"))


def test_validator_is_quiet_for_https_and_for_loopback_dev():
    assert _warnings_for("https://api.brevitassystems.com") == []
    assert _warnings_for("http://localhost:8000") == []
    assert _warnings_for("http://127.0.0.1:8000") == []


def test_validator_never_raises_and_warns_once():
    config_module._warned_base_urls.discard("http://api.example.com")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config_module.check_base_url("http://api.example.com")
        config_module.check_base_url("http://api.example.com")
    assert len(caught) == 1


def test_configure_keeps_the_safe_default_when_base_url_is_omitted():
    # Restore process-wide config: a leaked api_key would let another test's
    # report_usage reach the live control plane now that the default URL is real.
    snapshot = dict(config_module._cfg)
    try:
        before = config_module.get()["base_url"]
        config_module.configure(api_key="bvt_test_key")
        assert config_module.get()["base_url"] == before
    finally:
        config_module._cfg.clear()
        config_module._cfg.update(snapshot)


# ── `brevitas config` (finding: echoes the secret, persists nothing) ───────────

def test_config_command_masks_the_secret_and_admits_it_persists_nothing():
    result = CliRunner().invoke(main, ["config", "api-key", "bvt_live_supersecretvalue"])
    assert result.exit_code == 0
    output = _plain(result.output)
    assert "bvt_live_supersecretvalue" not in output
    assert "Nothing was written" in output
    assert "export BREVITAS_API_KEY=" in output


def test_config_command_prompts_when_the_value_is_not_on_argv():
    result = CliRunner().invoke(main, ["config", "api-key"], input="bvt_live_prompted\n")
    assert result.exit_code == 0
    assert "bvt_live_prompted" not in _plain(result.output)


def test_config_command_shows_base_url_and_flags_a_v1_suffix():
    config_module._warned_base_urls.discard("https://brevitassystems.com/v1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = CliRunner().invoke(main, ["config", "base-url", "https://brevitassystems.com/v1"])
    assert result.exit_code == 0
    assert "https://brevitassystems.com/v1" in _plain(result.output)   # not a secret
    assert any("/v1/v1" in str(w.message) for w in caught)


def test_config_command_rejects_unknown_keys():
    result = CliRunner().invoke(main, ["config", "nope", "x"])
    assert result.exit_code == 1


# ── `brevitas analyze --json` (finding: excerpts + absolute paths in reports) ──

def _analyze(tmp_path, *extra):
    (tmp_path / "app.py").write_text(
        'GOOGLE_API_KEY = "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ012345"\n'
        'SYSTEM = "You are a helpful marketing assistant"\n'
        "client.chat.completions.create(**kwargs)\n"
    )
    result = CliRunner().invoke(main, ["analyze", str(tmp_path), "--json", *extra])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_analyze_json_omits_excerpts_and_absolute_paths_by_default(tmp_path):
    payload = _analyze(tmp_path)
    assert payload["calls"], payload
    for call in payload["calls"]:
        assert "prompt_excerpt" not in call
        assert call["location"] == "app.py:3"
        assert str(tmp_path) not in call["location"]


def test_analyze_json_include_excerpts_is_opt_in_and_still_redacted(tmp_path):
    payload = _analyze(tmp_path, "--include-excerpts")
    excerpts = " ".join(c["prompt_excerpt"] for c in payload["calls"])
    assert "You are a helpful marketing assistant" in excerpts
    assert "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in excerpts


# ── `brevitas init --ai` (finding: silent source upload) ───────────────────────

def test_init_ai_asks_before_uploading_and_declining_uploads_nothing(tmp_path, monkeypatch):
    (tmp_path / "mystery.py").write_text("# " + ("x" * 400) + "\ndef go():\n    return 1\n")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-local-test-key")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("Deepseek_api_key", raising=False)

    calls = []
    import brevitas.scanner.ai_assist as ai_assist
    monkeypatch.setattr(ai_assist, "ai_classify_files",
                        lambda paths, **kw: calls.append(list(paths)) or [])

    result = CliRunner().invoke(main, ["init", str(tmp_path), "--ai"], input="n\n")
    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "will upload the contents" in output
    assert "api.openai.com" in output
    assert "mystery.py" in output
    assert calls == []            # declined -> nothing left the machine


def test_ai_assist_redacts_before_upload(tmp_path):
    from brevitas.scanner.ai_assist import _redact_source
    source = (
        "GOOGLE_API_KEY = 'AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ012345'\n"
        "def go():\n"
        "\tpassword = 'hunter2-not-a-real-secret'\n"
        "\treturn 1\n"
    )
    redacted = _redact_source(source)
    assert "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in redacted
    assert "hunter2-not-a-real-secret" not in redacted
    # line numbers and indentation must survive: the model reports line numbers.
    assert len(redacted.splitlines()) == 4
    assert redacted.splitlines()[2].startswith("\t")


def test_ai_backend_accepts_the_standard_deepseek_env_name(monkeypatch):
    from brevitas.scanner.ai_assist import backend_host
    monkeypatch.delenv("Deepseek_api_key", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    assert backend_host() == "https://api.deepseek.com/v1"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    assert backend_host("openai") == "https://api.openai.com/v1"


def test_no_backend_configured_means_no_upload(monkeypatch):
    from brevitas.scanner.ai_assist import ai_classify_files, backend_host
    for name in ("DEEPSEEK_API_KEY", "Deepseek_api_key", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert backend_host() is None
    assert ai_classify_files([pytest.importorskip("pathlib").Path("x.py")]) == []
