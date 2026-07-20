\set ON_ERROR_STOP on

-- Evidence-preserving rollback of only the new validation layer. Receipt
-- columns, authoritative usage, billing rows, trigger, RLS, and ACLs remain.
alter table public.usage_log
    drop constraint if exists usage_log_receipt_cache_tiers_check;
