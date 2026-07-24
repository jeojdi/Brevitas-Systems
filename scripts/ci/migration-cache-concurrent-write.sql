\set ON_ERROR_STOP on

select public.semantic_cache_store_bounded(
    lpad(to_hex(:cache_index), 64, '0'),
    repeat('6', 64),
    'concurrent:model',
    null::vector,
    'ciphertext-concurrent-' || :cache_index::text,
    repeat('5', 64),
    1,
    1,
    3600,
    3
);
