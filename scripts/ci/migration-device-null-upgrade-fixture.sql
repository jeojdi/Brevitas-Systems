\set ON_ERROR_STOP on

-- Simulate a pre-constraint quarantined receipt in a disposable DB. Removing
-- this check is test-only: a migration-010 reapply must erase recovery material
-- even when the original approver is known but the row is already quarantined.
insert into auth.users(id,email) values (
    'c1000000-0000-4000-8000-000000000001',
    'legacy-device-approver@example.invalid'
) on conflict(id) do nothing;
insert into public.organizations(id,name,legacy_owner_id,billing_owner_id) values (
    'c2000000-0000-4000-8000-000000000002','Legacy receipt tenant',
    'release-legacy-receipt-owner','c1000000-0000-4000-8000-000000000001'
) on conflict(id) do nothing;

alter table public.bvx_device_consumption_receipts
    drop constraint if exists bvx_device_receipt_ciphertext_check;
insert into public.bvx_device_consumption_receipts(
    device_hash,key_hash,encrypted_key,owner_id,approver_id,organization_id,
    consumed_at,expires_at,request_id,quarantined_at
) values (
    repeat('1',64),repeat('2',64),'legacy-kms-ciphertext',
    'c1000000-0000-4000-8000-000000000001',
    'c1000000-0000-4000-8000-000000000001',
    'c2000000-0000-4000-8000-000000000002',
    now(),now()+interval '10 minutes','release-quarantined-ciphertext',now()
) on conflict(device_hash) do update set
    encrypted_key=excluded.encrypted_key,
    approver_id=excluded.approver_id,
    quarantined_at=excluded.quarantined_at;
