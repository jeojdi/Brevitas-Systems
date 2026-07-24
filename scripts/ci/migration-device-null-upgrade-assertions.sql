\set ON_ERROR_STOP on

do $$
begin
    if not exists (
        select 1 from public.bvx_device_consumption_receipts receipt
         where receipt.device_hash=repeat('1',64)
           and receipt.approver_id='c1000000-0000-4000-8000-000000000001'
           and receipt.encrypted_key=''
           and receipt.quarantined_at is not null
    ) then
        raise exception 'migration 010 did not erase quarantined receipt ciphertext';
    end if;
    if not exists (
        select 1 from pg_constraint constraint_state
         where constraint_state.conrelid=
               'public.bvx_device_consumption_receipts'::regclass
           and constraint_state.conname='bvx_device_receipt_ciphertext_check'
           and constraint_state.convalidated
    ) then
        raise exception 'migration 010 did not validate its receipt ciphertext constraint';
    end if;
end;
$$;
