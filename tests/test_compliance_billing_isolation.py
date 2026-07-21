from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/202607200011_compliance_billing_isolation.sql"
ASSERTIONS = ROOT / "scripts/ci/migration-compliance-billing-isolation-assertions.sql"


def compact(text: str) -> str:
    return "".join(text.lower().split())


def test_compliance_exports_use_company_billing_identity():
    migration = MIGRATION.read_text()
    normalized = compact(migration)

    assert "begin;" in migration and migration.rstrip().endswith("commit;")
    assert "account.organization_id=p_organization_id" in normalized
    assert "ledger.organization_id=p_organization_id" in normalized
    assert "event.organization_id=p_organization_id" in normalized
    assert "account.user_id=v_request.subject_id" in normalized
    assert "ledger.user_id=v_request.subject_id" in normalized
    assert "event.user_id=v_request.subject_id" in normalized
    assert "notin('billing_account','billing_ledger','legacy_billing_event')" in normalized
    assert "whereaccount.user_id=v_request.subject_id;" not in normalized
    assert "whereledger.user_id=v_request.subject_idorderbyledger.id;" not in normalized


def test_compliance_cleanup_is_company_scoped_and_server_only():
    migration = MIGRATION.read_text()
    normalized = compact(migration)

    assert "organization.billing_owner_id=p_user_id" in normalized
    assert "whereaccount.organization_id=p_organization_id" in normalized
    assert "whereevent.organization_id=p_organization_id" in normalized
    assert "whereledger.organization_id=p_organization_id" in normalized
    assert "updatepublic.billing_accountssetcheckout_session_id=nullwhereuser_id=$1" not in normalized
    assert "updatepublic.billing_eventssetsession_id=''whereuser_id=$1" not in normalized
    for function in (
        "compliance_export_tenant",
        "compliance_export_subject",
        "compliance_delete_tenant",
        "compliance_delete_subject",
    ):
        signature = f"public.{function}(uuid,uuid,text)"
        assert f"grant execute on function {signature}" in migration
    assert "from public, anon, authenticated, service_role" in migration


def test_postgres_fixture_proves_shared_owner_isolation():
    assertions = ASSERTIONS.read_text()

    for marker in (
        "cus_compliance_isolation_a",
        "cus_compliance_isolation_b",
        "sub_compliance_isolation_b",
        "cs_compliance_isolation_b",
        "legacy_session_compliance_b",
        "legacy_session_tenant_ambiguous",
        "tenant A export contained company B or ambiguous billing evidence",
        "member subject export crossed the requested company boundary",
        "subject deletion mutated another company or lost financial evidence",
        "installed anonymizer retains owner-wide billing mutation",
    ):
        assert marker in assertions
    assert assertions.startswith("\\set ON_ERROR_STOP on\n")
    assert assertions.rstrip().endswith("rollback;")
