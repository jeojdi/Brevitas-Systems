-- Safe rollback for 202607170007. This file lives outside supabase/migrations
-- so migration runners never apply it as a forward migration. Rollback is
-- refused after any request, hold, or tombstone exists because dropping
-- compliance evidence is destructive.

do $$
begin
    if exists (select 1 from public.data_subject_requests)
       or exists (select 1 from public.legal_holds)
       or exists (select 1 from public.legal_hold_actions)
       or exists (select 1 from public.backup_deletion_tombstones)
       or exists (select 1 from public.compliance_retention_runs)
       or exists (select 1 from public.compliance_retention_worker_state) then
        raise exception 'compliance workflow rollback refused: retained evidence exists';
    end if;
end;
$$;

revoke all on function public.compliance_delete_tenant(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_delete_subject(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_replay_deletion_tombstone(text,uuid,uuid,timestamptz,timestamptz,text,uuid,text,text,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_run_retention(uuid,text,integer,boolean)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_retention_worker_cycle(uuid,uuid,uuid,uuid,text,text,integer)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_retention_worker_health()
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_complete_export(uuid,uuid,text,text,text,integer,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_export_tenant(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_export_subject(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_approve_legal_hold_action(uuid,uuid,text,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_request_legal_hold_action(uuid,uuid,text,uuid,text,text,text,text,timestamptz)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_approve_data_request(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_submit_data_request(uuid,uuid,text,text,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_submit_subject_request(uuid,uuid,text,text,uuid,text,text)
    from public, anon, authenticated, service_role;

drop function public.compliance_replay_deletion_tombstone(text,uuid,uuid,timestamptz,timestamptz,text,uuid,text,text,text);
drop function public.compliance_retention_worker_health();
drop function public.compliance_retention_worker_cycle(uuid,uuid,uuid,uuid,text,text,integer);
drop function public.compliance_run_retention(uuid,text,integer,boolean);
drop function public.compliance_retention_delete_immutable(timestamptz,integer);
drop function public.compliance_delete_subject(uuid,uuid,text);
drop function public.compliance_delete_tenant(uuid,uuid,text);
drop function public.compliance_complete_export(uuid,uuid,text,text,text,integer,text);
drop function public.compliance_export_subject(uuid,uuid,text);
drop function public.compliance_export_tenant(uuid,uuid,text);
drop function public.compliance_approve_legal_hold_action(uuid,uuid,text,text);
drop function public.compliance_request_legal_hold_action(uuid,uuid,text,uuid,text,text,text,text,timestamptz);
drop function public.compliance_approve_data_request(uuid,uuid,text);
drop function public.compliance_submit_subject_request(uuid,uuid,text,text,uuid,text,text);
drop function public.compliance_submit_data_request(uuid,uuid,text,text,text);
drop function public.compliance_anonymize_unshared_user(uuid);
drop function public.compliance_assert_usage_export_schema();
drop function public.compliance_erase_support_records(uuid,text,uuid);
drop function public.compliance_record_deadline_breach(public.data_subject_requests,text);
drop function public.compliance_request_has_hold(uuid,text);
drop function public.compliance_global_preservation_hold();
drop function public.compliance_preservation_hold(uuid);
drop function public.compliance_actor_role(text);

drop trigger legal_hold_actions_reject_truncate on public.legal_hold_actions;
drop trigger legal_hold_actions_enforce_transition on public.legal_hold_actions;
drop function public.enforce_legal_hold_action_transition();

drop trigger compliance_retention_runs_reject_truncate on public.compliance_retention_runs;
drop trigger compliance_retention_runs_reject_update_delete on public.compliance_retention_runs;
drop function public.reject_compliance_retention_run_mutation();
drop table public.compliance_retention_worker_state;
drop table public.compliance_retention_runs;

drop trigger backup_tombstones_reject_truncate on public.backup_deletion_tombstones;
drop trigger backup_tombstones_reject_update_delete on public.backup_deletion_tombstones;
drop function public.reject_backup_tombstone_mutation();
drop table public.backup_deletion_tombstones;
drop table public.legal_hold_actions;
drop table public.legal_holds;
drop table public.data_subject_requests;
