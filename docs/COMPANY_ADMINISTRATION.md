# Company administration

Company administration is tenant-derived: the API validates the Supabase bearer token, resolves the active membership from Postgres, and constructs `{actorId, companyId, role}` server-side. For invitation acceptance it also HMACs the verified Supabase identity email before entering the administration service. Browser-supplied company IDs, roles, or email claims are ignored. Production must set independent random `COMPANY_ADMIN_CURSOR_SECRET` and `COMPANY_ADMIN_INVITEE_PEPPER` values of at least 32 characters on every FastAPI replica, plus `BREVITAS_API_URL` on Vercel.

## Authorization matrix

| Capability | company_owner | company_admin | member | billing_admin |
|---|---:|---:|---:|---:|
| Read company/member roster | Yes | Yes | Yes | Yes |
| Invite member/admin/billing admin | Yes | Yes | No | No |
| Disable/remove ordinary members | Yes | Yes | No | No |
| Manage company admins | Yes | No | No | No |
| Promote/demote company owners | Yes | No | No | No |
| Read service accounts | Yes | Yes | No | No |
| Create/rotate/revoke service accounts | Yes | Yes | No | No |
| Read tenant key metadata through `/v1/keys` | Yes | Yes | Own dashboard session only | Own dashboard session only |
| Revoke dashboard sessions through `/v1/keys` | Yes | Yes | Own only | Own only |
| Manage billing | Yes | No | No | Yes |
| Read immutable administration audit | Yes | Yes | No | Yes |

The database locks and re-reads the actor membership for every mutation. A company admin cannot modify an owner or another company admin. The last active owner cannot be disabled, removed, or demoted; this check runs while owner rows are locked in the same transaction as the mutation and audit append.

## Lifecycle and credential rules

- Invitations expire within seven days, have a maximum of 100 pending records per company, and contain only a keyed email lookup plus a SHA-256 token digest. The raw invitation token is returned once with `Cache-Control: private, no-store`; no delivery is performed by this repository run. Acceptance requires both the token and an HMAC derived from the server-verified identity email. Wrong identities and replay are denied. Any existing membership in the target company—including disabled/removed members, admins, and owners—is rejected instead of being overwritten.
- Members are retained as `active`, `disabled`, or `removed` instead of being silently deleted. Re-enablement is explicit.
- `company_admin_active_memberships(p_actor_user_id,p_active_organization_id)` is the only company-choice source for authenticated dashboard/device UI. It verifies and locks the server-derived active membership, returns at most 100 active memberships for that same actor as `{company_id,company_name,role}`, and orders the active company first followed by deterministic name/ID order. Disabled/removed memberships, non-canonical roles, and other users' companies never enter the response. Only `service_role` may execute it.
- Service accounts have 1–12 allowlisted scopes, a required maximum 365-day expiry, and a maximum of 100 active, unexpired accounts per company. Runtime authentication must use `service_account_key_context` / `service_key_authorization`, which joins the key and account on their composite tenant identity and rejects either expiry/revocation state.
- Rotation rejects expired accounts, revokes every active key for that service account, and inserts the replacement hash in one transaction. Key expiry is clamped to the account expiry. Raw `bvt_…` keys are returned once and never persisted.
- List endpoints cap pages at 100 (the dashboard requests 50). Cursors are HMAC-authenticated, tenant- and collection-bound keyset positions. The dashboard stores cursor strings and never decodes them; previous navigation uses a cursor stack that resets when a view/filter/sort/range changes. `/v1/keys` follows the same rule and never returns `key_hash`, a hash-derived fingerprint, or a raw credential. Owners/admins may see metadata for tenant keys; members/billing admins see only dashboard sessions they created.

## Audit evidence

Every accepted administration mutation and every authorization/state denial records only:

`request_id`, opaque `actor_id`, `organization_id`, fixed `action`, fixed `target_type`, opaque `target_id`, `actor_role`, `outcome`, and timestamp.

Names, email addresses, prompts, responses, request bodies, invitation tokens, API keys, full hashes, and `actor_key_hash` are excluded. Actor roles come from a finite allowlist; actor/target IDs are bounded opaque identifiers. The database rejects email/credential/secret patterns and full 64-character digests even from direct service-role inserts. Key events target an opaque key row ID, short fingerprint, or parent service-account ID—never the credential or full hash. Application telemetry emits only the fixed `admin_audit_committed` event without identifiers.

`admin_audit_committed` is emitted only after an audited mutation RPC/service call returns from its immutable database transaction. Its sole field is the finite outcome `success` or, for mutation endpoints whose denial paths always append evidence, `rejected`; it never includes request, actor, company, target, email, or payload data. A bounded 15-minute/4,096-entry process-local receipt suppresses duplicate telemetry for the same request ID without becoming authoritative state. Telemetry failures are swallowed after the audit commit and never alter or retry the administration response.

RLS is enabled, but immutability does not rely on RLS: `BEFORE UPDATE OR DELETE` and `BEFORE TRUNCATE` triggers reject mutations for every role, including `service_role`. A database owner can still perform a monitored break-glass DDL operation, so Supabase database-owner access must be restricted and alerted separately. Audit retention is 400 days; removal after that period requires an approved evidence-retention procedure, not ordinary application credentials.

## API composition

The FastAPI composition root must include `api.company_admin.router` and call:

```python
configure_company_admin(
    company_admin_for_store(_store),
    verified_company_principal,
    lambda request: request.state.brevitas_request_id,
)
```

`verified_company_principal` must validate the Supabase user and query the active membership. The module intentionally returns 503 until configured. Vercel's `/api/admin/company/**` route is only a bounded BFF: it forwards the bearer token and request ID to Railway, enforces a 64 KiB request limit, a 1 MiB streamed response limit, and an eight-second timeout, and never accepts company/role headers. Every local error carries `private, no-store` and `nosniff` headers.

For invitation acceptance, the resolver may return an authenticated actor with an empty current company, but it must set `invitee_lookup_hash=service.invitee_lookup(verified_supabase_email)`. The accepted company ID is normalized from the locked invitation row. All list methods call one authorization-plus-keyset RPC; service-role table reads are forbidden.

### Atomic key/audit contract (migrations 008 and 009)

W3 store methods must generate the raw key in process, retain it only for the one-time response, and call these RPCs instead of separate `api_keys` and `audit_events` requests:

- `company_admin_create_dashboard_session_key(p_organization_id, p_actor_user_id, p_key_hash, p_key_prefix, p_expires_at, p_request_id)` returns `{ok,key_id,organization_id,key_type,scopes,environment,prefix,expires_at}`. It authorizes the active DB role, serializes the company cap, revokes the actor's prior dashboard session, inserts only the digest, and appends an audit event targeting `key_id` in one transaction.
- `company_admin_dashboard_keys_page(p_organization_id, p_actor_user_id, p_cursor_time, p_cursor_id, p_limit, p_request_id)` returns `{ok,items}` with at most `min(max(limit,1),100)+1` rows ordered by `(created,id)` descending. Each item is limited to `{id,name,created,key_type,scopes,environment,prefix,service_account_id,expires_at,last_used_at,revoked_at}`. Owners/admins receive tenant metadata allowed by the matrix; members/billing admins receive only their own `dashboard_session` rows. The RPC locks and re-reads the active role in the same transaction as the keyset query.
- `company_admin_revoke_dashboard_session_key(p_organization_id, p_actor_user_id, p_key_id, p_request_id)` returns `{ok,key_id,revoked,already_revoked}`. Owners/admins may revoke tenant dashboard sessions; members/billing admins may revoke only their own. Non-dashboard, cross-tenant, other-owner, missing, and disabled-membership cases share `{ok:false,code:"forbidden_or_not_found"}` and content-free denial evidence. Row lock, mutation, and audit append are one transaction.

Callers must treat any `{ok:false,code}` as a failed operation and must never cache or return the raw key in that case. The RPC arguments and audit records never contain the raw credential; `p_key_hash` is used only for the `api_keys` insert and is never an audit target.

Migration 009 revokes and drops migration 008's `company_admin_revoke_key(uuid,uuid,uuid,text)` surface. Do not restore it: its generic name and owner/admin branch could address service-account keys. Service-account credential lifecycle remains exclusively behind `company_admin_rotate_service_key` and `company_admin_revoke_service_account`.

#### W1/W3 `/v1/keys` composition contract

- `GET /v1/keys?cursor=<opaque>&limit=<1..100>` derives organization and actor from the verified session, validates a maximum 512-character cursor, and returns `{keys,next_cursor,has_more,limit}`. W3 verifies an HMAC-authenticated cursor bound to the organization and `dashboard_keys` collection, passes only its `(created,id)` position to `company_admin_dashboard_keys_page`, trims the extra row, and signs the next position. It must not issue a service-role `GET api_keys`.
- `DELETE /v1/keys/{key_id}` validates the UUID and directly calls `company_admin_revoke_dashboard_session_key`; it must not pre-list the key. A service-account ID and a cross-tenant ID use the same generic denied response. Long-lived credentials continue to use the company service-account endpoints.
- `POST /v1/keys` with `purpose=dashboard_session` continues to use the migration 008 create RPC. The cloud endpoint rejects long-lived/service purpose and directs that workflow to company service accounts.

The exact frozen migration 009 SQL signatures are:

```sql
public.company_admin_dashboard_keys_page(
  uuid,uuid,timestamptz,uuid,integer,text
)
public.company_admin_revoke_dashboard_session_key(uuid,uuid,uuid,text)
```

The exact frozen migration 011 SQL signature is:

```sql
public.company_admin_active_memberships(uuid,uuid)
```

`GET /v1/company/capabilities` includes `companies` from that RPC alongside its server-derived `company_id`, role, and permissions. The dashboard accepts device selectors only from this authenticated bounded list, defaults a single-company user to the active company, clears the list and selection on an authentication-user change, and submits `company_id` in the approval body. The device approval endpoint independently revalidates that exact active membership; the browser list is never authorization.

## Explicit evidence-preserving rollback

Rollback is a controlled database-owner operation. Never drop `audit_events`, its immutable triggers, or the archive schema. First stop administration writes and export evidence to encrypted, access-logged storage. Then run:

Migration 009 is forward-only for authorization evidence. Put `/v1/keys` list/revoke into maintenance-deny mode before removing its strict RPCs. Do not restore direct service-role table reads or the generic migration 008 revoke function:

```sql
begin;
revoke all on function public.company_admin_dashboard_keys_page(
  uuid,uuid,timestamptz,uuid,integer,text) from service_role;
revoke all on function public.company_admin_revoke_dashboard_session_key(
  uuid,uuid,uuid,text) from service_role;
drop function if exists public.company_admin_dashboard_keys_page(
  uuid,uuid,timestamptz,uuid,integer,text);
drop function if exists public.company_admin_revoke_dashboard_session_key(
  uuid,uuid,uuid,text);
commit;
```

Before rolling back migration 011, disable multi-company device approval in the UI. Do not replace its authenticated list with browser metadata or a user-entered company ID:

```sql
begin;
revoke all on function public.company_admin_active_memberships(uuid,uuid)
  from service_role;
drop function if exists public.company_admin_active_memberships(uuid,uuid);
commit;
```

Migration 008 is forward-only for key/audit rows. Roll application callers back first, preserve every key and audit row, then remove only its remaining callable RPC:

```sql
begin;
revoke all on function public.company_admin_create_dashboard_session_key(
  uuid,uuid,text,text,timestamptz,text) from service_role;
drop function if exists public.company_admin_create_dashboard_session_key(
  uuid,uuid,text,text,timestamptz,text);
commit;
```

Do not restore keys revoked by a replacement or revoke call: doing so would revive credentials whose raw value may have escaped its intended lifetime. Audit evidence remains under migration 005 immutability/retention controls.

For a full company-administration rollback, continue with the evidence archive and migration 005 RPC removal:

```sql
begin;
lock table public.audit_events in access exclusive mode;

-- The migration creates this archive table with the source identity/constraints.
select public.archive_company_administration_audit();

-- Verify an exact evidence copy before removing callable administration RPCs.
do $$
declare source_count bigint; archive_count bigint;
begin
  select count(*) into source_count from public.audit_events;
  select count(*) into archive_count
    from audit_evidence_archive.company_admin_audit;
  if source_count <> archive_count then
    raise exception 'audit archive count mismatch: source %, archive %',
      source_count, archive_count;
  end if;
end $$;

drop function if exists public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text);
drop function if exists public.company_admin_service_accounts_page(uuid,uuid,timestamptz,uuid,integer,text);
drop function if exists public.company_admin_invitations_page(uuid,uuid,timestamptz,uuid,integer,text);
drop function if exists public.company_admin_members_page(uuid,uuid,timestamptz,uuid,integer,text);
drop function if exists public.company_admin_active_memberships(uuid,uuid);
drop function if exists public.company_admin_dashboard_keys_page(uuid,uuid,timestamptz,uuid,integer,text);
drop function if exists public.company_admin_revoke_dashboard_session_key(uuid,uuid,uuid,text);
drop function if exists public.company_admin_revoke_service_account(uuid,uuid,uuid,text);
drop function if exists public.service_key_authorization(text);
drop function if exists public.company_admin_rotate_service_key(uuid,uuid,uuid,text,text,timestamptz,text);
drop function if exists public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],timestamptz,text);
drop function if exists public.company_admin_set_member(uuid,uuid,uuid,text,text,text);
drop function if exists public.company_admin_accept_invitation(uuid,text,text,text);
drop function if exists public.company_admin_cancel_invitation(uuid,uuid,uuid,text);
drop function if exists public.company_admin_invite_member(uuid,uuid,uuid,text,text,text,timestamptz,text);
drop function if exists public.lock_company_admin_namespace(uuid);
drop function if exists public.lock_company_actor_role(uuid,uuid);
drop function if exists public.company_role_permissions(text);

-- Revoke active service-account keys before disabling the UI/API rollout.
update public.api_keys
   set revoked_at = coalesce(revoked_at, now())
 where service_account_id is not null;
update public.service_accounts
   set status='revoked', revoked_at=coalesce(revoked_at,now()), updated_at=now()
 where status='active';

-- Invitations contain no raw delivery secret and are made unusable.
update public.organization_invitations
   set status='cancelled', cancelled_at=coalesce(cancelled_at,now())
 where status='pending';

commit;
```

Preserve `organization_members` lifecycle columns, `organization_invitations`, `service_accounts`, `audit_events`, and `audit_evidence_archive.company_admin_audit` until the 400-day audit-retention obligation and any legal hold have expired. Export the archive with checksums before any later schema cleanup. Reverting canonical roles requires a separately reviewed compatibility migration because deployed APIs may already depend on the new names.

## Staging gates

Before enterprise launch, apply migrations through 011 in staging, configure all server-only environment variables, exercise every role and denial path, verify wrong-email/replay/existing-member invitation rejection, race both 100-row caps from multiple connections, verify runtime account/key expiry joins, verify `/v1/keys` own-vs-admin visibility and dashboard-only revocation, verify active-company choices exclude disabled/foreign memberships and stop at 100, verify raw secrets and key hashes never appear in list responses/logs/traces, attempt malicious audit INSERT plus UPDATE/DELETE/TRUNCATE with the service role, test cursor tampering/cross-tenant replay, and restore the encrypted audit export into an isolated database.
