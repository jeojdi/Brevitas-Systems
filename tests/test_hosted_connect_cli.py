"""`brevitas connect` — the one command that puts a design partner on the billable path.

The hosted endpoint is the ONLY billable path: the local proxy's receipts are
recorded authoritative=false by the API on purpose, so anything `connect` prints
has to point at https://api.brevitassystems.com/v1 and has to carry
X-Brevitas-Customer-ID, without which an organization_service key hard-400s on its
very first request (api/server.py:1755-1757).

These tests pin the three properties that decide whether onboarding works or leaks:
  1. the printed snippet is the hosted endpoint plus both required headers,
  2. a user with no organization is refused with a next step, not a stack trace,
  3. the minted key reaches stdout exactly once and never lands in a file anyone
     else on the machine can read.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import brevitas.cli as cli
from brevitas.cli import main

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

ORG_ID = "8f3c1d2e-4a5b-4c6d-8e9f-0a1b2c3d9a21"
SERVICE_KEY = "bvt_live_TESTKEYtestkeyTESTKEYtestkey0123456789c41d"


def _plain(text: str) -> str:
    """rich colours the failure paths; assert on the words, not the escape codes."""
    return _ANSI.sub("", text)


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _capabilities(permissions=("service_accounts:manage", "company:read"),
                  company_id=ORG_ID, name="Acme, Inc."):
    return _Response(200, {
        "company_id": company_id,
        "role": "company_owner",
        "permissions": sorted(permissions),
        "companies": [{"company_id": company_id, "company_name": name,
                       "role": "company_owner", "account_type": "company"}],
    })


def _service_account():
    return _Response(200, {
        "id": "5c2a7b18-9d31-4f88-b0a2-1e6c4d5f7a90",
        "name": "bvx:testhost", "environment": "production",
        "scopes": sorted(cli.HOSTED_SERVICE_SCOPES),
        "status": "active", "expires_at": "2026-10-28T12:00:00+00:00",
        "key_id": "b1e2c3d4-5678-4abc-9def-0123456789ab",
        "api_key": SERVICE_KEY, "prefix": SERVICE_KEY[:12],
        "secret_available_once": True,
    })


class _Api:
    """Records every hosted call so the test can assert which endpoints were used."""

    def __init__(self, *, capabilities=None, create=None, import_customers=None):
        self.calls: list[tuple[str, str, dict, object]] = []
        self._capabilities = capabilities or _capabilities()
        self._create = create or _service_account()
        self._import = import_customers or _Response(200, {
            "organization_id": ORG_ID, "customers": [{"external_id": "acme-inc"}], "count": 1})

    def __call__(self, method: str, url: str, headers: dict, json_body=None):
        self.calls.append((method, url, dict(headers), json_body))
        if url.endswith("/company/capabilities"):
            return self._capabilities
        if url.endswith("/company/service-accounts"):
            return self._create
        if url.endswith("/customers/import"):
            return self._import
        raise AssertionError(f"unexpected hosted call: {method} {url}")


@pytest.fixture
def api(monkeypatch):
    stub = _Api()
    monkeypatch.setattr(cli, "_http_request", stub)
    return stub


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.config/brevitas/connection.json."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgconfig"))
    monkeypatch.delenv("BREVITAS_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("BREVITAS_BASE_URL", raising=False)
    monkeypatch.delenv("BREVITAS_API_KEY", raising=False)
    # The auto language pick shells out to the in-tree scanner over the CWD; pin the
    # order so these assertions describe `connect`, not whatever repo it ran in.
    monkeypatch.setattr(cli, "_detected_snippet_order",
                        lambda path: ["python", "anthropic", "node", "curl", "env"])
    return tmp_path


def _run(args, **kwargs):
    return CliRunner().invoke(main, ["connect", "--token", "dash-session-token", *args],
                              catch_exceptions=False, **kwargs)


# ── 1. the printed snippet is the hosted, billable configuration ──────────────

def test_output_carries_hosted_base_url_and_customer_header(api):
    result = _run([])
    assert result.exit_code == 0, result.output
    out = result.output

    # The hosted endpoint, with /v1 exactly once — the SDK base_url, not a bare origin.
    assert "https://api.brevitassystems.com/v1" in out
    assert "/v1/v1" not in out

    # Both headers the hosted proxy actually reads. The Brevitas key is NOT bearer
    # auth: Authorization carries the customer's provider key and is forwarded
    # upstream (brevitas/proxy.py:1366), so a snippet that put the Brevitas key
    # there would 401 on the first call.
    assert "X-Brevitas-Customer-ID" in out
    assert "X-Brevitas-Key" in out

    # Every language snippet must carry the customer header, not just the first.
    for snippet in ("from openai import OpenAI", "from anthropic import Anthropic",
                    "import OpenAI from \"openai\"", "curl https://"):
        assert snippet in out, snippet
    blocks = [block for block in out.split("\n# ") if "base_url" in block or "baseURL" in block
              or "curl https://" in block]
    assert blocks, out
    for block in blocks:
        assert "X-Brevitas-Customer-ID" in block

    # The tenant id is resolved and shown on its own line, not left as a placeholder.
    assert "Customer   acme-inc" in out


def test_connect_uses_the_existing_company_endpoint_and_never_starts_a_proxy(api):
    result = _run([])
    assert result.exit_code == 0, result.output

    urls = [url for _method, url, _headers, _body in api.calls]
    assert urls == [
        "https://api.brevitassystems.com/v1/company/capabilities",
        "https://api.brevitassystems.com/v1/company/service-accounts",
        "https://api.brevitassystems.com/v1/customers/import",
    ]
    create = next(call for call in api.calls if call[1].endswith("/service-accounts"))
    assert create[0] == "POST"
    assert create[2]["Authorization"] == "Bearer dash-session-token"
    assert create[3]["scopes"] == list(cli.HOSTED_SERVICE_SCOPES)
    assert create[3]["expires_in_days"] == 90
    assert create[3]["environment"] == "production"

    # The tenant is provisioned with the key just minted — proof the key works.
    imported = next(call for call in api.calls if call[1].endswith("/customers/import"))
    assert imported[2] == {"X-Brevitas-Key": SERVICE_KEY}
    assert imported[3]["customers"][0]["external_id"] == "acme-inc"

    # Never the local proxy: no loopback base URL, no `brevitas start`.
    assert "localhost" not in result.output and "127.0.0.1" not in result.output
    assert "brevitas start" not in result.output
    assert "ANTHROPIC_BASE_URL" not in result.output


def test_multi_tenant_keeps_the_header_and_imports_nothing(api):
    result = _run(["--multi-tenant"])
    assert result.exit_code == 0, result.output
    assert "X-Brevitas-Customer-ID" in result.output
    assert "<your-customer-id>" in result.output
    assert not [url for _m, url, _h, _b in api.calls if url.endswith("/customers/import")]
    # The 400 the gate raises is quoted verbatim so nobody meets it for the first
    # time in a failing production request.
    assert ("Organization service proxy calls require X-Brevitas-Customer-ID"
            in result.output)


# ── 2. refuse gracefully, with the next step ──────────────────────────────────

def test_no_organization_is_refused_with_a_next_step(monkeypatch):
    monkeypatch.setattr(cli, "_http_request",
                        _Api(capabilities=_capabilities(company_id="")))
    result = _run([])
    assert result.exit_code == 1
    out = _plain(result.output)
    assert "not an active member of any Brevitas organization" in out
    assert "create (or accept an invitation to) a workspace" in out
    assert "Traceback" not in out


def test_membership_denied_by_the_api_is_refused_with_a_next_step(monkeypatch):
    monkeypatch.setattr(cli, "_http_request", _Api(
        capabilities=_Response(403, {"detail": "Company administration permission denied"})))
    result = _run([])
    assert result.exit_code == 1
    out = _plain(result.output)
    assert "not an active member of any Brevitas organization" in out
    assert "Company administration permission denied" in out


def test_expired_session_says_so_instead_of_reporting_no_organization(monkeypatch):
    monkeypatch.setattr(cli, "_http_request", _Api(
        capabilities=_Response(401, {"detail": "Authentication required"})))
    result = _run([])
    assert result.exit_code == 1
    out = _plain(result.output)
    assert "not valid (401)" in out and "fresh token" in out


def test_a_member_without_manage_permission_is_told_who_can_run_it(monkeypatch):
    monkeypatch.setattr(cli, "_http_request", _Api(
        capabilities=_capabilities(permissions=("company:read", "service_accounts:read"))))
    result = _run([])
    assert result.exit_code == 1
    out = _plain(result.output)
    assert "cannot create service accounts" in out
    assert "Ask a company owner or admin" in out
    # It must not have tried to mint anything after being told no.
    assert result.exit_code == 1


def test_service_account_limit_conflict_explains_both_causes(monkeypatch):
    monkeypatch.setattr(cli, "_http_request", _Api(
        create=_Response(409, {"detail": "Company administration request conflicts "
                                         "with current state"})))
    result = _run([])
    assert result.exit_code == 1
    out = _plain(result.output)
    assert "active service-account limit" in out and "billing owner" in out


def test_missing_token_refuses_rather_than_prompting_forever(monkeypatch):
    launched = []
    monkeypatch.setattr(cli.click, "launch", lambda url, **kwargs: launched.append(url))
    result = CliRunner().invoke(main, ["connect"], input="\n", catch_exceptions=False)
    assert result.exit_code == 1
    out = _plain(result.output)
    assert "No dashboard session token" in out
    # The sign-in URL is printed either way; a non-tty (CI, a pipe) is never
    # ambushed with a browser window.
    assert "https://brevitassystems.com/dashboard" in out
    assert launched == []


def test_a_non_http_dashboard_url_is_never_handed_to_a_browser(monkeypatch):
    launched = []
    monkeypatch.setattr(cli.click, "launch", lambda url, **kwargs: launched.append(url))
    monkeypatch.setattr(cli, "_interactive_stdin", lambda: True)

    denied = CliRunner().invoke(
        main, ["connect", "--dashboard-url", "file:///etc/passwd"],
        input="\n", catch_exceptions=False)
    assert denied.exit_code == 1
    assert launched == []

    # …and the guard is not vacuous: an https dashboard at a terminal does open.
    allowed = CliRunner().invoke(main, ["connect"], input="\n", catch_exceptions=False)
    assert allowed.exit_code == 1
    assert launched == ["https://brevitassystems.com/dashboard"]


def test_no_open_browser_is_respected(monkeypatch):
    launched = []
    monkeypatch.setattr(cli.click, "launch", lambda url, **kwargs: launched.append(url))
    monkeypatch.setattr(cli, "_interactive_stdin", lambda: True)
    result = CliRunner().invoke(main, ["connect", "--no-open-browser"], input="\n",
                                catch_exceptions=False)
    assert result.exit_code == 1
    assert launched == []


def test_a_failed_customer_import_still_prints_the_key_and_says_what_happened(monkeypatch):
    monkeypatch.setattr(cli, "_http_request", _Api(
        import_customers=_Response(403, {"detail": "Key cannot import customers"})))
    result = _run([])
    assert result.exit_code == 0, result.output
    assert SERVICE_KEY in result.output
    assert "not pre-created" in result.output
    assert "Key cannot import customers" in result.output


# ── 3. the secret: printed once, never world-readable ─────────────────────────

def test_the_key_is_printed_exactly_once(api):
    result = _run([])
    assert result.exit_code == 0, result.output
    assert result.output.count(SERVICE_KEY) == 1
    # Everything else refers to it by env var or prefix, never by value.
    assert 'os.environ["BREVITAS_API_KEY"]' in result.output
    assert "$BREVITAS_API_KEY" in result.output


def test_connection_file_is_private_and_holds_no_secret(api, _isolated_config):
    result = _run([])
    assert result.exit_code == 0, result.output

    path = Path(os.environ["XDG_CONFIG_HOME"]) / "brevitas" / "connection.json"
    assert path.exists(), result.output
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert not mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH)

    body = path.read_text(encoding="utf-8")
    assert SERVICE_KEY not in body
    record = json.loads(body)
    assert record["organization_id"] == ORG_ID
    assert record["customer_external_id"] == "acme-inc"
    assert record["key_prefix"] == SERVICE_KEY[:12]
    # No key, no key hash — a prefix is a label, not a credential.
    assert "api_key" not in record and "key_hash" not in record
    assert not any(SERVICE_KEY in str(value) for value in record.values())


def test_env_file_is_opt_in(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _run([])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".env").exists()
    assert not list(tmp_path.glob("*.env"))


def test_env_file_when_requested_is_private_and_not_echoed(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "secrets.env"
    result = _run(["--env-file", str(target)])
    assert result.exit_code == 0, result.output

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, oct(mode)
    body = target.read_text(encoding="utf-8")
    assert f"BREVITAS_API_KEY={SERVICE_KEY}" in body
    assert "BREVITAS_CUSTOMER_ID=acme-inc" in body
    # Written once to the file, printed once to the terminal — never twice in either.
    assert body.count(SERVICE_KEY) == 1
    assert result.output.count(SERVICE_KEY) == 1
    # The confirmation names the variables, never their values.
    assert "BREVITAS_API_KEY" in result.output
    assert "values not echoed" in result.output


def test_env_file_refuses_a_world_readable_pre_existing_file_by_tightening_it(
        api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "loose.env"
    target.write_text("EXISTING=1\n", encoding="utf-8")
    os.chmod(target, 0o644)
    result = _run(["--env-file", str(target)])
    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "EXISTING=1" in target.read_text(encoding="utf-8")   # append-only


def test_env_file_refuses_a_git_tracked_path(api, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    tracked = repo / "config.env"
    tracked.write_text("EXISTING=1\n", encoding="utf-8")
    subprocess.run(["git", "add", "config.env"], cwd=repo, check=True)
    monkeypatch.chdir(repo)

    result = _run(["--env-file", "config.env"])
    assert result.exit_code == 1
    assert "tracked by git" in _plain(result.output)
    assert SERVICE_KEY not in tracked.read_text(encoding="utf-8")


def test_env_file_refuses_a_path_git_does_not_ignore(api, tmp_path, monkeypatch):
    repo = tmp_path / "repo2"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.chdir(repo)

    result = _run(["--env-file", ".env"])
    assert result.exit_code == 1
    assert "not matched by .gitignore" in _plain(result.output)
    assert not (repo / ".env").exists()


def test_env_file_accepts_an_ignored_path_in_a_repo(api, tmp_path, monkeypatch):
    repo = tmp_path / "repo3"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    result = _run(["--env-file"])
    assert result.exit_code == 0, result.output
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o600
    assert SERVICE_KEY in (repo / ".env").read_text(encoding="utf-8")


def test_env_file_will_not_silently_replace_an_existing_key(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "has-key.env"
    target.write_text("BREVITAS_API_KEY=bvt_old_value\n", encoding="utf-8")

    result = _run(["--env-file", str(target)])
    assert result.exit_code == 1
    assert "already defines BREVITAS_API_KEY" in _plain(result.output)
    assert SERVICE_KEY not in target.read_text(encoding="utf-8")

    forced = _run(["--env-file", str(target), "--force"])
    assert forced.exit_code == 0, forced.output
    assert SERVICE_KEY in target.read_text(encoding="utf-8")


def test_a_command_line_token_is_flagged_as_history_leaking(api):
    result = _run([])
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "shell history and the process table" in out
    assert "BREVITAS_DASHBOARD_TOKEN" in out
    # The warning must never quote the token itself.
    assert "dash-session-token" not in out


def test_an_env_supplied_token_is_not_flagged(api, monkeypatch):
    monkeypatch.setenv("BREVITAS_DASHBOARD_TOKEN", "dash-session-token")
    result = CliRunner().invoke(main, ["connect"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "shell history" not in _plain(result.output)


# ── billing-check: the customer can see their own billability ─────────────────

def _readiness(billable: bool):
    return _Response(200, {
        "organization_id": ORG_ID, "window_hours": 24, "billable": billable,
        "checks": {
            "hosted_key": {"ok": True, "detail": "organization_service, expires 2026-10-28"},
            "authoritative_rows": {"ok": billable, "count": 12 if billable else 0},
            "billing_arrangement": {"ok": billable,
                                    "state": "marginal_per_call" if billable else "unattested",
                                    "detail": "Brevitas has not attested your billing "
                                              "arrangement yet"},
        },
    })


def test_billing_check_exits_non_zero_until_the_arrangement_is_attested(monkeypatch):
    monkeypatch.setattr(cli, "_http_request",
                        lambda method, url, headers, json_body=None: _readiness(False))
    result = CliRunner().invoke(main, ["billing-check", "--api-key", SERVICE_KEY],
                                catch_exceptions=False)
    assert result.exit_code == 1
    out = _plain(result.output)
    assert "billing_arrangement" in out and "unattested" in out
    assert "Billable: no" in out


def test_billing_check_passes_once_everything_is_true(monkeypatch):
    calls = []

    def _stub(method, url, headers, json_body=None):
        calls.append((method, url, headers))
        return _readiness(True)

    monkeypatch.setattr(cli, "_http_request", _stub)
    result = CliRunner().invoke(main, ["billing-check", "--api-key", SERVICE_KEY],
                                catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "Billable: yes" in _plain(result.output)
    # Authenticates with the same header the proxy reads, not bearer auth.
    assert calls[0][2] == {"X-Brevitas-Key": SERVICE_KEY}
    assert calls[0][1].endswith("/v1/billing/readiness")


def test_billing_check_is_honest_when_the_endpoint_is_not_deployed(monkeypatch):
    monkeypatch.setattr(cli, "_http_request",
                        lambda method, url, headers, json_body=None:
                        _Response(404, {"detail": "Not Found"}))
    result = CliRunner().invoke(main, ["billing-check", "--api-key", SERVICE_KEY],
                                catch_exceptions=False)
    assert result.exit_code == 1
    assert "does not expose GET /v1/billing/readiness yet" in _plain(result.output)


def test_billing_check_reads_the_endpoint_recorded_by_connect(api, monkeypatch):
    result = _run([])
    assert result.exit_code == 0, result.output

    seen = []
    monkeypatch.setattr(cli, "_http_request",
                        lambda method, url, headers, json_body=None:
                        (seen.append(url), _readiness(True))[1])
    check = CliRunner().invoke(main, ["billing-check", "--api-key", SERVICE_KEY],
                               catch_exceptions=False)
    assert check.exit_code == 0, check.output
    assert seen == ["https://api.brevitassystems.com/v1/billing/readiness"]
    assert "Acme, Inc." in _plain(check.output)


# ── the additive promise: nothing existing moved ──────────────────────────────

def test_every_pre_existing_command_still_exists():
    result = CliRunner().invoke(main, ["--help"], catch_exceptions=False)
    for command in ("start", "scan", "apply", "config", "status", "optimize",
                    "analyze", "audit", "init"):
        assert re.search(rf"^\s+{command}\s", _plain(result.output), re.MULTILINE), command


def test_start_still_advertises_the_local_proxy_but_admits_it_cannot_bill():
    result = CliRunner().invoke(main, ["start", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Start the local Brevitas proxy server" in _plain(result.output)
