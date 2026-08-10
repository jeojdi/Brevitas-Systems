-- Phase 1.5 learned warming (202608100006): chain-hash prefix keying, the
-- prefix tree, its erasure posture and its retention horizon.
--
-- Everything runs inside one transaction and rolls back, so the file is
-- rerunnable and leaves no fixture row behind.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-00000010f001';
    v_org uuid := '00000000-0000-4000-8000-00000010f002';
    v_cust uuid := '00000000-0000-4000-8000-00000010f003';
    v_peer uuid := '00000000-0000-4000-8000-00000010f004';
    v_request uuid := '00000000-0000-4000-8000-00000010f005';
    v_export_request uuid := '00000000-0000-4000-8000-00000010f006';
    v_hash_a text := repeat('c1', 32);
    v_hash_b text := repeat('c2', 32);
    v_observe_signature text := 'public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text,jsonb,text,integer)';
    v_digest_root text := repeat('a0', 32);
    v_digest_one text := repeat('a1', 32);
    v_digest_two text := repeat('a2', 32);
    v_digest_alt text := repeat('b1', 32);
    v_digest_alt_two text := repeat('b2', 32);
    v_chain jsonb;
    v_chain_alt jsonb;
    v_path text;
    v_path_alt text;
    v_count integer;
    v_touch timestamptz;
    v_leaf text;
    v_label text;
    v_result jsonb;
    v_policy_count integer;
begin
    -- ------------------------------------------------------------------
    -- 0. Signature and privilege surface. The eleven-argument form is REPLACED
    -- (three defaulted arguments on top of it would make an eleven-argument
    -- call ambiguous), so what is pinned here is that the old shape is gone.
    -- ------------------------------------------------------------------
    if to_regprocedure(v_observe_signature) is null
       or to_regprocedure('public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text)') is not null then
        raise exception 'warm_prefix_observe must be the fourteen-argument form';
    end if;
    if has_function_privilege('anon', v_observe_signature, 'execute')
       or has_function_privilege('authenticated', v_observe_signature, 'execute')
       or not has_function_privilege('service_role', v_observe_signature, 'execute') then
        raise exception 'warm_prefix_observe privileges are wrong after 202608100006';
    end if;

    -- ------------------------------------------------------------------
    -- 1. Table posture. RLS on, ZERO policies, no direct grants: the tree is
    -- reachable only through the SECURITY DEFINER writer.
    -- ------------------------------------------------------------------
    if to_regclass('public.warm_prefix_node') is null
       or to_regclass('public.warm_prefix_edge') is null then
        raise exception '202608100006 did not create the prefix tree tables';
    end if;
    if not (select relrowsecurity from pg_catalog.pg_class
             where oid = 'public.warm_prefix_node'::regclass)
       or not (select relrowsecurity from pg_catalog.pg_class
                where oid = 'public.warm_prefix_edge'::regclass) then
        raise exception 'the prefix tree tables must have RLS enabled';
    end if;
    select count(*) into v_policy_count from pg_catalog.pg_policies
     where schemaname = 'public'
       and tablename in ('warm_prefix_node', 'warm_prefix_edge');
    if v_policy_count <> 0 then
        raise exception 'the prefix tree tables must carry zero policies';
    end if;
    if has_table_privilege('anon', 'public.warm_prefix_node', 'select')
       or has_table_privilege('authenticated', 'public.warm_prefix_node', 'select')
       or has_table_privilege('anon', 'public.warm_prefix_edge', 'select')
       or has_table_privilege('authenticated', 'public.warm_prefix_edge', 'select') then
        raise exception 'the prefix tree tables must not be directly readable';
    end if;

    -- ltree lives in `extensions` and the path column is that type: a btree
    -- path index would hit the index-tuple wall on a deep chain, so the path
    -- index must be gist.
    if to_regtype('extensions.ltree') is null then
        raise exception 'ltree must be installed in the extensions schema';
    end if;
    if not exists (
        select 1 from pg_catalog.pg_index index
          join pg_catalog.pg_class relation on relation.oid = index.indexrelid
          join pg_catalog.pg_am access on access.oid = relation.relam
         where index.indrelid = 'public.warm_prefix_node'::regclass
           and access.amname = 'gist'
    ) then
        raise exception 'warm_prefix_node.path must carry a gist index';
    end if;
    foreach v_label in array array['chain_leaf_digest', 'chain_path',
                                   'chain_salt_version'] loop
        if not exists (
            select 1 from pg_catalog.pg_attribute
             where attrelid = 'public.warm_prefixes'::regclass
               and attname = v_label
               and not attisdropped
        ) then
            raise exception 'warm_prefixes is missing %', v_label;
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- 2. Fixture. One organization, one enabled credential, two customers.
    -- ------------------------------------------------------------------
    insert into auth.users (id, email)
    values (v_actor, 'warm-chain-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm chain fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'warm-chain-subject'),
           (v_peer, v_org, 'warm-chain-peer');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);

    v_path := 's1.k1.r' || substr(v_digest_root, 1, 12) || '.'
              || substr(v_digest_one, 1, 12) || '.'
              || substr(v_digest_two, 1, 12);
    v_chain := jsonb_build_array(
        jsonb_build_object('digest', v_digest_root, 'label', substr(v_digest_root, 1, 12),
                           'parent', '', 'depth', 0, 'block_tokens', 0,
                           'token_cum', 0, 'block_elements', 0,
                           'path_truncated', false),
        jsonb_build_object('digest', v_digest_one, 'label', substr(v_digest_one, 1, 12),
                           'parent', v_digest_root, 'depth', 1, 'block_tokens', 1200,
                           'token_cum', 1200, 'block_elements', 3,
                           'path_truncated', false),
        jsonb_build_object('digest', v_digest_two, 'label', substr(v_digest_two, 1, 12),
                           'parent', v_digest_one, 'depth', 2, 'block_tokens', 900,
                           'token_cum', 2100, 'block_elements', 2,
                           'path_truncated', false));

    -- ------------------------------------------------------------------
    -- 3. An observation carrying a chain writes nodes, edges and the stamp.
    -- ------------------------------------------------------------------
    v_result := public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash_a, 'enc:payload', 40000, 300, 60,
        false, 0.5, 'claude', v_chain, v_path, 1);
    if v_result ->> 'status' <> 'observed' then
        raise exception 'the chain observation did not record an arrival';
    end if;
    select count(*) into v_count from public.warm_prefix_node node
     where node.organization_id = v_org;
    if v_count <> 3 then
        raise exception 'expected three chain nodes, found %', v_count;
    end if;
    select count(*) into v_count from public.warm_prefix_edge edge
     where edge.organization_id = v_org;
    if v_count <> 2 then
        raise exception 'expected two chain edges, found %', v_count;
    end if;
    if (select node.path::text from public.warm_prefix_node node
         where node.node_digest = v_digest_root and node.organization_id = v_org)
       <> 's1.k1.r' || substr(v_digest_root, 1, 12) then
        raise exception 'the root node path must be the header plus the root label';
    end if;
    if (select node.path::text from public.warm_prefix_node node
         where node.node_digest = v_digest_two and node.organization_id = v_org)
       <> v_path then
        raise exception 'the leaf node path must be the full arm path';
    end if;
    -- Containment is now a path test, which is the entire point of the tree.
    select count(*) into v_count from public.warm_prefix_node node
     where node.organization_id = v_org
       -- The operator is schema qualified deliberately: ltree lives in
       -- `extensions`, which is not on any pinned search_path, so every future
       -- reader of this tree must qualify it exactly like this.
       and node.path operator(extensions.<@)
           ('s1.k1.r' || substr(v_digest_root, 1, 12))::extensions.ltree;
    if v_count <> 3 then
        raise exception 'ltree containment did not find the subtree (%)', v_count;
    end if;
    select prefix.chain_leaf_digest, prefix.chain_salt_version
      into v_leaf, v_count
      from public.warm_prefixes prefix
     where prefix.organization_id = v_org and prefix.prefix_hash = v_hash_a;
    if v_leaf <> v_digest_two or v_count <> 1 then
        raise exception 'the arm was not stamped with its chain leaf';
    end if;

    -- ------------------------------------------------------------------
    -- 4. A second observation TOUCHES; it never duplicates.
    -- ------------------------------------------------------------------
    update public.warm_prefix_node node set last_touch_at = now() - interval '1 hour'
     where node.organization_id = v_org;
    select min(node.last_touch_at) into v_touch from public.warm_prefix_node node
     where node.organization_id = v_org;
    perform public.warm_prefix_observe(
        v_org, v_peer, 'anthropic', v_hash_b, 'enc:payload', 40000, 300, 60,
        false, 0.5, 'claude', v_chain, v_path, 1);
    select count(*) into v_count from public.warm_prefix_node node
     where node.organization_id = v_org;
    if v_count <> 3 then
        raise exception 'a shared chain must be touched, not duplicated (%)', v_count;
    end if;
    if (select min(node.last_touch_at) from public.warm_prefix_node node
         where node.organization_id = v_org) <= v_touch then
        raise exception 'the second observation did not touch the shared nodes';
    end if;
    -- Two customers, one shared subtree: exactly the containment the dedup
    -- pre-pass and the attribution job are built on.
    select count(distinct prefix.customer_id) into v_count
      from public.warm_prefixes prefix
     where prefix.organization_id = v_org and prefix.chain_path = v_path;
    if v_count <> 2 then
        raise exception 'both customers should be stamped onto the shared path';
    end if;

    -- ------------------------------------------------------------------
    -- 5. Malformed chains are skipped TOTALLY, and never fail the arrival.
    -- ------------------------------------------------------------------
    v_result := public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', repeat('c3', 32), 'enc:payload', 40000, 300,
        60, false, 0.5, 'claude',
        jsonb_set(v_chain, '{2,depth}', to_jsonb(7)), v_path, 1);
    if v_result ->> 'status' <> 'observed' then
        raise exception 'a malformed chain must not fail the observation';
    end if;
    select count(*) into v_count from public.warm_prefix_node node
     where node.organization_id = v_org;
    if v_count <> 3 then
        raise exception 'a malformed chain must write no nodes at all (%)', v_count;
    end if;
    if (select prefix.chain_leaf_digest from public.warm_prefixes prefix
         where prefix.organization_id = v_org
           and prefix.prefix_hash = repeat('c3', 32)) is not null then
        raise exception 'a malformed chain must not stamp its arm';
    end if;

    -- ------------------------------------------------------------------
    -- 6. The collision escape. Two distinct chains landing on the same labels
    -- must SEPARATE, not overwrite: the second one extends its own label.
    -- ------------------------------------------------------------------
    v_path_alt := v_path;
    v_chain_alt := jsonb_build_array(
        jsonb_build_object('digest', v_digest_alt, 'label', substr(v_digest_root, 1, 12),
                           'parent', '', 'depth', 0, 'block_tokens', 0,
                           'token_cum', 0, 'block_elements', 0,
                           'path_truncated', false),
        jsonb_build_object('digest', v_digest_alt_two, 'label', substr(v_digest_one, 1, 12),
                           'parent', v_digest_alt, 'depth', 1, 'block_tokens', 800,
                           'token_cum', 800, 'block_elements', 2,
                           'path_truncated', false));
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', repeat('c4', 32), 'enc:payload', 40000, 300,
        60, false, 0.5, 'claude', v_chain_alt,
        's1.k1.r' || substr(v_digest_root, 1, 12) || '.'
        || substr(v_digest_one, 1, 12), 1);
    select node.label into v_label from public.warm_prefix_node node
     where node.organization_id = v_org and node.node_digest = v_digest_alt;
    if v_label is null then
        raise exception 'the colliding chain wrote no root node';
    end if;
    if v_label <> substr(v_digest_alt, 1, 12) || '_' || substr(v_digest_alt, 13, 4) then
        raise exception 'the colliding node did not take the extended label (%)', v_label;
    end if;
    select count(*) into v_count from public.warm_prefix_node node
     where node.organization_id = v_org;
    if v_count <> 5 then
        raise exception 'the colliding subtree must sit beside the original (%)', v_count;
    end if;

    -- ------------------------------------------------------------------
    -- 7. Retention. Nodes age on the 90-day touch horizon; an edge whose child
    -- is gone goes with it.
    -- ------------------------------------------------------------------
    update public.warm_prefix_node node
       set last_touch_at = now() - interval '91 days'
     where node.organization_id = v_org and node.depth > 0;
    v_result := public.purge_warm_state(7);
    if coalesce((v_result ->> 'prefix_nodes_deleted')::integer, 0) < 3 then
        raise exception 'retention did not prune the stale chain nodes (%)', v_result;
    end if;
    select count(*) into v_count from public.warm_prefix_edge edge
     where edge.organization_id = v_org;
    if v_count <> 0 then
        raise exception 'an edge whose child was pruned must not survive (%)', v_count;
    end if;

    -- ------------------------------------------------------------------
    -- 8. Tenant erasure takes the tree with it. Re-seed first: step 7 pruned
    -- the descendants.
    -- ------------------------------------------------------------------
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash_a, 'enc:payload', 40000, 300, 60,
        false, 0.5, 'claude', v_chain, v_path, 1);
    select count(*) into v_count from public.warm_prefix_node node
     where node.organization_id = v_org;
    if v_count < 3 then
        raise exception 'the re-seeded chain is missing nodes (%)', v_count;
    end if;

    -- The tenant export reports the tree as COUNTS and never as digests: a node
    -- digest is a salted, content-derived identifier of one block of one
    -- customer's prompt prefix.
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        v_export_request, v_org, 'export', 'tenant', 'approved',
        'evidence:warm-chain:export', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-chain',
        'system:warm-chain');
    select count(*) into v_count
      from public.compliance_export_tenant(
            v_org, v_export_request, 'brevitas_admin:assertion') record
     where record ->> 'record_type' = 'warming_prefix_tree';
    if v_count < 1 then
        raise exception 'the tenant export must report the prefix tree';
    end if;
    if exists (
        select 1 from public.compliance_export_tenant(
            v_org, v_export_request, 'brevitas_admin:assertion') record
         where record ->> 'record_type' = 'warming_prefix_tree'
           and record::text like '%' || v_digest_root || '%'
    ) then
        raise exception 'the tenant export must never disclose node digests';
    end if;

    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        v_request, v_org, 'delete', 'tenant', 'approved',
        'evidence:warm-chain:tenant', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-chain',
        'system:warm-chain');
    perform public.compliance_delete_tenant(
        v_org, v_request, 'brevitas_admin:assertion');
    select count(*) into v_count from public.warm_prefix_node node
     where node.organization_id = v_org;
    if v_count <> 0 then
        raise exception 'tenant erasure left % chain nodes behind', v_count;
    end if;
    select count(*) into v_count from public.warm_prefix_edge edge
     where edge.organization_id = v_org;
    if v_count <> 0 then
        raise exception 'tenant erasure left % chain edges behind', v_count;
    end if;

    raise notice 'warm chain assertions passed';
end;
$$;

rollback;
