# Dual-Stack Deploy Runbook — unblocking Railway production

**Status:** ready to execute. **Written:** 2026-07-28.
**Scope:** Railway project `Brevitas Production` (`cf99d772-f71e-4d62-85e2-a7bc8f2e96c2`), environment `production`.

Every command in this runbook is prefixed with `!` because production writes must be
executed by the operator, not by an assistant session. Read-only Railway commands
(`deployment list`, `logs`, `variables`) work from either side.

---

## 1. Root cause

Railway's healthcheck prober connects to a new container **over IPv4**. Railway's private
service mesh (`*.railway.internal`) resolves **over IPv6**. A single listener has to serve
both, and `uvicorn --host` cannot do it:

| bind | IPv4 healthcheck | IPv6 mesh |
| --- | --- | --- |
| `--host 0.0.0.0` | passes | `NO_SOCKET` — API can't reach compressor |
| `--host ::` | **connection refused → "service unavailable"** | works |
| explicit socket, `IPV6_V6ONLY=0` | passes | works |

`--host ::` is IPv6-**only** because CPython's asyncio sets `IPV6_V6ONLY=1` on every
`AF_INET6` socket it creates, so the v4-mapped address space (`::ffff:0:0/96`) never gets a
listener. Commit `cff0d15` switched all three services from `0.0.0.0` to `::` to fix mesh
reachability, and from that moment **every** deploy of new code failed its healthcheck.

The evidence, verbatim from `railway logs <deployId> -b`:

```
====================
Starting Healthcheck
====================
Path: /ready
Retry window: 5m0s
Attempt #1 failed with service unavailable. Continuing to retry for 4m49s
...
Attempt #11 failed with service unavailable. Continuing to retry for 8s
1/1 replicas never became healthy!
Healthcheck failed!
```

The app itself was fine — it booted, loaded the model, and reported ready. Nothing could
reach it on IPv4.

**The fix** (three parallel changes, all now in the working tree):

| service | file | mechanism |
| --- | --- | --- |
| compressor | `services/compress/serve.py` + `services/compress/Dockerfile` (`CMD ["python","serve.py"]`) | commit `371f49a` |
| API | `api/serve.py` + root `Dockerfile` (`CMD [".../start-with-adc.sh","python","-m","api.serve"]`) | uncommitted |
| worker | `api/worker.py:829-844` (binds the health socket itself, hands the fd to uvicorn) | uncommitted |

Each one creates `socket(AF_INET6)`, calls
`setsockopt(IPPROTO_IPV6, IPV6_V6ONLY, 0)`, binds `("::", PORT)`, and passes the descriptor
to uvicorn — one socket, both address families, independent of kernel defaults.

### Current production state (measured 2026-07-28)

```
$ curl -s https://api.brevitassystems.com/v1/version
{"service":"api","build":{"commit_sha":"cff0d159afcae5be5fef41fc3eeb7605ea08ace2"}}
$ curl -s https://brevitassystems.com/api/version
{"service":"dashboard","build":{"commit_sha":"f8043224b32f16fa82e3735dd035e8b720cbee88"}}
```

Vercel is 4 commits ahead of the backend. That single fact is the whole dashboard outage:
`/v1/stats/cache` (`api/server.py:4038`) and `/v1/audit` (`api/server.py:4458`) exist in
`f804322` but not in the `cff0d15` image that production is still serving, so both return
**404** and the new dashboard tabs render empty.

Last-good deployments still serving traffic:

| service | last SUCCESS | since |
| --- | --- | --- |
| `Brevitas-Systems` | `a1c042b2-e2cc-4fb2-b1df-cdd268d5e010` | 2026-07-27 03:30 PT |
| `worker-production` | `048b38b3-3747-4277-b811-db2377cd8ef7` | 2026-07-27 14:51 PT |
| `compressor-production` | `254fdfb6-ac43-4c90-871f-c9e4f6f8b522` | 2026-07-24 04:00 PT (last `0.0.0.0` build) |

---

## 2. Deploy order — compressor → worker → API

**This order is forced by the API's own readiness handler, not by convention.**

`api/server.py:4734`

```python
@app.get("/v1/health")
@app.get("/v1/health/ready")
async def health():
    compressor = await _compressor_status()
    compressor_healthy = all(
        compressor[name] for name in (
            "configured", "internal_auth_configured", "private_endpoint", "reachable",
            "model_loaded",
        )
    )
    compressor_required = os.getenv(
        "BREVITAS_COMPRESS_REQUIRED", "false").lower() in {"1", "true", "yes"}
```

and `api/server.py:4764`, `4796`:

```python
    compressor_blocks_readiness = compressor_required and not compressor_healthy
    ...
    return payload if core_ready and not compressor_blocks_readiness else JSONResponse(
        payload, status_code=503)
```

Production has the switch **on** — verified with `railway variables -s Brevitas-Systems`:

```
BREVITAS_COMPRESS_REQUIRED = true
BREVITAS_COMPRESS_URL      = <set, *.railway.internal>
BREVITAS_COMPRESS_TOKEN    = <set>
```

`reachable` and `model_loaded` come from a live HTTP call into the mesh
(`api/server.py:4649`):

```python
response = _requests.get(f"{url}/ready", timeout=(timeout, timeout))
if response.ok:
    data["reachable"] = True
    data["model_loaded"] = bool(response.json().get("model_loaded"))
```

**Therefore: if the compressor is not answering `/ready` over `*.railway.internal` with
`model_loaded: true`, the API returns 503 on `/v1/health/ready` and its own Railway
healthcheck fails.** Deploying the API first would fail for a reason that has nothing to do
with the API's own bug. The compressor must be green first.

Two more constraints from the code:

- **The URL must be private.** `_private_compressor_url` (`api/server.py:4616-4631`) returns
  `True` only for hosts ending in `.railway.internal` (loopback is dev-only), and
  `_validate_runtime_config` (`api/server.py:880-886`) raises
  `"Production BREVITAS_COMPRESS_URL must use Railway private networking"` at boot
  otherwise. So the API genuinely depends on the **IPv6 mesh** path, which is exactly what
  `0.0.0.0` would have broken — the dual-stack socket is the only bind that satisfies both
  the prober and this probe.
- **The worker is independent.** `api/worker.py:222-245` gates `/ready` on
  `_WORKER_ACCEPTING and database_ready and redis_ready and kms_ready` — Postgres, Redis,
  KMS. It never touches the compressor or the API. It can safely go in parallel with the
  compressor; it is sequenced second only to keep one variable in flight at a time.

Final order: **compressor → worker → API.**

---

## 3. Prerequisite: get all three fixes onto `main`

Railway auto-deploys from GitHub branch `main` on all three services, and that is the only
acceptable trigger.

> **`railway up` is forbidden.** It uploads a local snapshot with no git metadata, so
> `RAILWAY_GIT_COMMIT_SHA` is never injected and `api/build_info.py` aborts the boot with
> `RuntimeError: Production requires a full immutable build commit SHA`. Deploys must come
> from GitHub. The only valid CLI trigger is
> `railway redeploy --from-source -s <service>`, which pulls the latest commit from the
> configured source.

As of writing, the three fixes are **not** all on `origin/main`:

```
$ git log --oneline origin/main..main
371f49a fix(compress): dual-stack socket so Railway healthchecks and IPv6 mesh both work
```

`main` is ahead of `origin/main` by one unpushed commit (the compressor fix), and the API +
worker fixes are still uncommitted working-tree changes (`api/serve.py`, root `Dockerfile`,
`api/worker.py`).

Land them, then push:

```bash
# Confirm what you're about to ship.
! git status --short
! git log --oneline origin/main..main

# Commit the API + worker dual-stack fixes (on a branch, then merge to main as usual),
# so that main contains 371f49a plus both new fixes.

# Sanity-check that main really has all three before pushing.
! git show main:services/compress/Dockerfile | grep CMD          # expect: python serve.py
! git show main:Dockerfile | grep CMD                            # expect: python -m api.serve
! git show main:api/worker.py | grep -n IPV6_V6ONLY              # expect: one hit

# Single push triggers auto-deploy on ALL THREE services simultaneously.
! git push origin main
! git rev-parse main   # record this SHA — every verification below compares against it
```

**Note the coupling:** one push to `main` fires all three builds at once, so the strict
compressor-before-API ordering below cannot be enforced by pushing alone. That is fine and
expected — the API build takes minutes and the compressor's cached rebuild takes seconds, so
the compressor is normally healthy well before the API's healthcheck window opens. If the
API's healthcheck nonetheless fails while the compressor is still coming up, do **not**
debug the API: wait for the compressor to go green, then re-trigger the API alone with the
targeted redeploy in §4.3. The previous API deployment keeps serving throughout.

---

## 4. Deploy

### 4.1 Compressor (`compressor-production`)

Config: `deploy/railway.json` → `dockerfilePath services/compress/Dockerfile`,
`healthcheckPath /ready`, `healthcheckTimeout 300` (**5m0s retry window**, confirmed in the
build log above), `numReplicas 1`.

```bash
! railway redeploy --from-source -s compressor-production -e production -y
```

**Expected build time: seconds to ~1 minute, not minutes.** The torch install
(`RUN pip install -r scripts/ci/python-compressor.lock`, ~2 GB / 3-4 GB image per the
Dockerfile comment) is layer **4 of 8**; `371f49a` only changes layer 5
(`COPY app.py serve.py ./`) and `CMD`, both *after* it, so the expensive layer stays cached.
The last failed deploy (`112541f6`) shows all eight layers cache-hit and reached
`Starting Healthcheck` ~5 s after the deploy was created. **Only a change to
`scripts/ci/python-compressor.lock` forces a cold torch download — budget 15-25 min in that
case,** which is why the retry window is 5m0s rather than 2m0s: the model also loads on first
boot (30-120 s per the Dockerfile `HEALTHCHECK --start-period=120s`).

**Success looks like:** `Healthcheck succeeded!` in the build log, status `SUCCESS`, and —
this is the load-bearing bit — the API's own readiness reporting the compressor as reachable.

```bash
! railway deployment list -s compressor-production --limit 3
# expect the newest row: SUCCESS, meta.commitHash == the SHA you pushed
```

Do not proceed until this is `SUCCESS`.

### 4.2 Worker (`worker-production`)

Config: `deploy/railway-worker.json` → root `Dockerfile`, `healthcheckPath /ready`,
`healthcheckTimeout 120` (**2m0s retry window**), `numReplicas 2`, start command
`/usr/local/bin/start-with-adc.sh env BREVITAS_WORKER_BILLING_ROLE=authoritative python -m api.worker`.

```bash
! railway redeploy --from-source -s worker-production -e production -y
```

**Expected build time: ~3-8 minutes** (shared root image; `scripts/ci/python-runtime.lock`
installs from manylinux wheels, no compiler). Healthcheck window is tight at 2m0s but the
worker has no model to load — it only needs Postgres, Redis and KMS to answer.

**Success looks like:** `Healthcheck succeeded!`, status `SUCCESS`, and **both** replicas
healthy (a failure prints `2/2 replicas never became healthy!`).

```bash
! railway deployment list -s worker-production --limit 3
```

### 4.3 API (`Brevitas-Systems`)

Config: `railway.toml` / `railway.json` → root `Dockerfile`,
`healthcheckPath /v1/health/ready`, `healthcheckTimeout 120` (**2m0s retry window**),
`numReplicas 2`. Public at `api.brevitassystems.com`.

```bash
! railway redeploy --from-source -s Brevitas-Systems -e production -y
```

**Expected build time: ~3-8 minutes**, usually faster than the worker's because the two
share the root image layers.

**Success looks like:** `Healthcheck succeeded!`, `SUCCESS`, both replicas healthy, and
`/v1/health/ready` returning **200** with `"status": "ok"` and `compressor.reachable: true`.

```bash
! railway deployment list -s Brevitas-Systems --limit 3
```

---

## 5. Post-deploy verification

Run all of §5 top to bottom. Nothing here writes.

### 5.0 Capture the deployment IDs

```bash
! export EXPECTED_SHA="$(git rev-parse main)"
! export API_DEPLOY="$(railway deployment list -s Brevitas-Systems --limit 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
! export WORKER_DEPLOY="$(railway deployment list -s worker-production --limit 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
! export COMPRESS_DEPLOY="$(railway deployment list -s compressor-production --limit 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
! echo "sha=$EXPECTED_SHA api=$API_DEPLOY worker=$WORKER_DEPLOY compress=$COMPRESS_DEPLOY"
```

### 5.1 Status + commit per service

```bash
! for s in Brevitas-Systems worker-production compressor-production; do \
    printf '%-24s ' "$s"; \
    railway deployment list -s "$s" --limit 1 --json \
      | python3 -c 'import json,sys; d=json.load(sys.stdin)[0]; print(d["status"], d["meta"].get("commitHash","?")[:8], d["meta"].get("branch","?"))'; \
  done
```

Pass criteria: all three `SUCCESS`, all three commit hashes equal to
`${EXPECTED_SHA:0:8}`, branch `main`.

### 5.2 Healthcheck actually PASSED (the critical assertion)

A green `SUCCESS` row is necessary but not sufficient evidence that the dual-stack fix
worked — assert on the healthcheck line itself. Note the healthcheck transcript lands in the
**build** log (`-b`), not the deploy log (`-d`).

```bash
! railway logs "$COMPRESS_DEPLOY" -s compressor-production -b --lines 400 \
    | grep -E "Retry window|Attempt #|Healthcheck succeeded|Healthcheck failed|never became healthy"
! railway logs "$WORKER_DEPLOY"   -s worker-production   -b --lines 400 \
    | grep -E "Retry window|Attempt #|Healthcheck succeeded|Healthcheck failed|never became healthy"
! railway logs "$API_DEPLOY"      -s Brevitas-Systems    -b --lines 800 \
    | grep -E "Retry window|Attempt #|Healthcheck succeeded|Healthcheck failed|never became healthy"
```

**Pass:** each block contains `Healthcheck succeeded!`.
**Fail:** any `Attempt #N failed with service unavailable`, `Healthcheck failed!`, or
`N/N replicas never became healthy!` — the bind is still wrong on that service.

### 5.3 The app is listening (runtime log)

```bash
! railway logs "$COMPRESS_DEPLOY" -s compressor-production -d --lines 120 \
    | grep -Ei "Uvicorn running|Application startup complete|compressor_model_loaded|Traceback|Error"
! railway logs "$WORKER_DEPLOY"   -s worker-production   -d --lines 120 \
    | grep -Ei "Uvicorn running|Application startup complete|worker|Traceback|Error"
! railway logs "$API_DEPLOY"      -s Brevitas-Systems    -d --lines 120 \
    | grep -Ei "Uvicorn running|Application startup complete|Traceback|RuntimeError"
```

Expect `Application startup complete.` per replica and, on the compressor,
`compressor_model_loaded`. Any `RuntimeError: Production requires a full immutable build
commit SHA` means the deploy did not come from GitHub — see the `railway up` warning in §3.

### 5.4 The 404s are gone

Both endpoints require authentication (`api/server.py:4040`, `4460`:
`kh: str = Depends(_authenticated)`), so an unauthenticated probe returns **401/403 —
which is the pass condition.** `404` means the old image is still serving.

```bash
! for p in /v1/stats/cache /v1/audit; do \
    printf '%-20s ' "$p"; \
    curl -s -o /dev/null -w '%{http_code}\n' "https://api.brevitassystems.com$p"; \
  done
```

| result | meaning |
| --- | --- |
| `401` or `403` | **PASS** — route exists, auth is enforced |
| `404` | **FAIL** — still on old code |
| `502`/`503` | API not up yet, or all replicas unhealthy |

Baseline before this deploy: both returned `404`.

### 5.5 Readiness still reports every dependency ready

```bash
! curl -s https://api.brevitassystems.com/v1/health/ready | python3 -m json.tool
```

Required:

```json
{
  "status": "ok",
  "accepting_traffic": true,
  "database_ready": true,
  "redis_ready": true,
  "kms_ready": true,
  "compressor": {
    "configured": true,
    "internal_auth_configured": true,
    "private_endpoint": true,
    "reachable": true,
    "model_loaded": true
  }
}
```

`compressor.reachable: true` alongside a passing IPv4 healthcheck is the **end-to-end proof
that one socket now serves both families**: the prober got in over IPv4 and the API got out
over the IPv6 mesh. If `reachable` is `false` the whole document returns HTTP 503 (see
`compressor_blocks_readiness`, §2) and the API's healthcheck will fail on the next restart —
treat it as a live incident even if the endpoint currently answers.

Note `_COMPRESSOR_TTL = 30.0` (`api/server.py:4604`): the compressor probe is cached for 30
seconds, so allow half a minute after a compressor change before re-reading this on an
already-running API.

Also assert liveness is independent (dependency outages must never trigger a restart storm):

```bash
! curl -s -o /dev/null -w '%{http_code}\n' https://api.brevitassystems.com/v1/health/live   # expect 200
```

### 5.6 Frontend and backend agree on the commit

```bash
! echo "expected : $EXPECTED_SHA"
! printf 'dashboard: '; curl -s https://brevitassystems.com/api/version \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["build"]["commit_sha"])'
! printf 'api      : '; curl -s https://api.brevitassystems.com/v1/version \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["build"]["commit_sha"])'
! printf 'worker   : '; railway logs "$WORKER_DEPLOY" -s worker-production -b --lines 5 >/dev/null; \
    railway deployment list -s worker-production --limit 1 --json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["meta"]["commitHash"])'
```

All three must print the same SHA as `$EXPECTED_SHA`. A mismatch on `dashboard` means Vercel
hasn't finished promoting; a mismatch on `api` means the redeploy did not land.

### 5.7 One-line green/red summary

```bash
! ( set -e; \
    for s in Brevitas-Systems worker-production compressor-production; do \
      st="$(railway deployment list -s "$s" --limit 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["status"])')"; \
      [ "$st" = SUCCESS ] || { echo "RED: $s is $st"; exit 1; }; \
    done; \
    cc="$(curl -s -o /dev/null -w '%{http_code}' https://api.brevitassystems.com/v1/stats/cache)"; \
    [ "$cc" != 404 ] || { echo "RED: /v1/stats/cache still 404"; exit 1; }; \
    ac="$(curl -s -o /dev/null -w '%{http_code}' https://api.brevitassystems.com/v1/audit)"; \
    [ "$ac" != 404 ] || { echo "RED: /v1/audit still 404"; exit 1; }; \
    rc="$(curl -s -o /dev/null -w '%{http_code}' https://api.brevitassystems.com/v1/health/ready)"; \
    [ "$rc" = 200 ] || { echo "RED: /v1/health/ready is $rc"; exit 1; }; \
    echo "GREEN: 3/3 SUCCESS, stats/cache=$cc audit=$ac ready=$rc" )
```

### 5.8 Optional: watch the IPv4 vs IPv6 split directly

If a healthcheck still fails and you want to see which family the prober used:

```bash
! railway logs -s Brevitas-Systems --network --since 2026-07-28T00:00:00Z --until 2026-07-28T23:59:59Z | head -50
```

---

## 6. Rollback

**There is nothing to roll back, and a failed deploy is safe to attempt.**

Railway only shifts traffic to a new deployment *after* its healthcheck passes. A deployment
that fails its healthcheck is marked `FAILED` and never receives traffic — the previous
healthy deployment keeps serving, untouched. That is precisely why production has been
stable on `cff0d15` through fourteen consecutive failed API deploys since 2026-07-24: users
saw an old-but-working API, not an outage.

Consequences:

- **Retrying costs nothing but build minutes.** If §5.2 shows `Healthcheck failed!`, fix the
  code, push, and redeploy. Traffic never moved.
- **No `railway rollback` step is needed** for a failed deploy.
- **The only real risk window** is a deploy that *passes* the healthcheck and is then found
  broken in a way the healthcheck doesn't cover. In that case redeploy the last known-good
  deployment from the Railway dashboard (Deployments → the last `SUCCESS` row → Redeploy).
  Last-good IDs are tabulated in §1.
- **`restartPolicyType = "ON_FAILURE"`, `restartPolicyMaxRetries = 10`** on all three
  services: a container that crashes *after* going healthy is restarted up to ten times
  before Railway gives up, so a transient dependency blip self-heals.
- **Ordering safety:** because a failed API deploy leaves the old API serving, deploying the
  API before the compressor is green is *wasteful* (a guaranteed 2m0s healthcheck failure),
  not *dangerous*.

---

## 7. Open question — `warm_*` tables return 403 to the service-role key

### Symptom

Querying `warm_credentials`, `warm_prefixes`, or `warm_budget_ledger` through PostgREST on
Supabase project `wyfz` with the **service role** key returns:

```
HTTP 403  {"code":"42501", ... "hint":"Grant the required privileges to the current role"}
```

while `usage_log` returns `200` under the same key. The tables exist; the migrations applied
cleanly.

### Conclusion: intentional, self-asserting, and harmless at runtime. No migration is needed.

**1. The revocation is deliberate and documented in the migration's own header.**

`supabase/migrations/202607280001_cache_warming.sql:1-5`:

> Predictive cache warming: org-scoped opt-in credentials, per-customer prefix
> observations, and a reservation-then-settle budget ledger. **Every table is
> RLS-enabled with zero policies and zero direct DML for any PostgREST role:
> all reads and writes flow through the security-definer RPCs below** so budget
> accounting, caps, and eviction share one serialized critical section.

`supabase/migrations/202607280001_cache_warming.sql:99-104` executes exactly that:

```sql
alter table public.warm_credentials enable row level security;
alter table public.warm_prefixes enable row level security;
alter table public.warm_budget_ledger enable row level security;
revoke all on table public.warm_credentials from public, anon, authenticated, service_role;
revoke all on table public.warm_prefixes from public, anon, authenticated, service_role;
revoke all on table public.warm_budget_ledger from public, anon, authenticated, service_role;
```

`service_role` is named explicitly in all three `revoke` statements. This is not an omission
— it is a removal of the Supabase project default. Contrast `usage_log`, which is
deliberately re-granted in
`supabase/migrations/202607220001_service_role_data_plane.sql:57` (revoke) and `:84`
(grant back) — hence its `200`.

**2. The migration refuses to apply if the grants ever come back.**

`supabase/migrations/202607280001_cache_warming.sql:770-808` is a self-verifying privilege
contract that loops over the three tables × `{service_role, anon, authenticated}` ×
`{SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER}` and aborts the whole
transaction on any hit:

```sql
if has_table_privilege(grantee, format('public.%I', contract_table), privilege) then
    raise exception 'unsafe % privilege contract for public.%: %',
        grantee, contract_table, privilege;
```

`supabase/migrations/202607280003_multi_provider_warming.sql:627-660` re-asserts the same
contract after superseding the RPCs. **Granting `SELECT` to `service_role` to "fix" the 403
would make both migrations fail on the next fresh-database run and break CI.** The 403 *is*
the contract holding.

**3. The backend never touches these tables directly — it only calls the RPCs.**

The hosted (Supabase/PostgREST) store in `api/store.py` reaches warming state through seven
RPC endpoints and nothing else:

| `api/store.py` | call |
| --- | --- |
| `:4187` | `POST rpc/warm_prefix_observe` |
| `:4208` | `POST rpc/warm_due_claim` |
| `:4235` | `POST rpc/warm_ping_settle` |
| `:4266` | `POST rpc/warm_credentials_upsert` |
| `:4278` | `POST rpc/warm_credentials_purge` |
| `:4283` | `POST rpc/warm_read_status` |
| `:4288` | `POST rpc/purge_warm_state` |

A grep for direct PostgREST table access (`_request(..., "warm_prefixes")`,
`"warm_credentials?..."`, etc.) across `api/store.py` returns **zero** hits. The code even
states the invariant in place, at `api/store.py:4173`:

```python
def warm_enabled(self, organization_id: str, customer_id: str = "") -> bool:
    # No PostgREST role may SELECT warm_credentials; warm_read_status is
    # the only read path. Callers cache this per-org answer.
```

Every RPC is `security definer` with `search_path` pinned and `grant execute ... to
service_role` — e.g. `202607280003_multi_provider_warming.sql:185-192`:

```sql
$$ language plpgsql security definer set search_path = pg_catalog, public;
revoke all on function public.warm_prefix_observe(...) from public, anon, authenticated;
grant execute on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric
) to service_role;
```

So `service_role` has **execute** on the functions and **nothing** on the tables. The
functions run as their owner and reach the tables that way. This is the standard
security-definer pattern, and it is what lets budget accounting, per-org caps, and eviction
share one serialized critical section instead of racing across replicas.

The SQLite fallback store (`api/store.py:1333-1338`, `:2813-3236`) *does* hit the tables
directly with raw SQL, but that path is local-dev/self-host only and never speaks PostgREST.
`brevitas/warming.py` is pure computation — it extracts and hashes cacheable prefixes
(`extract_warm_prefix`, `:228`) and holds the observer callback slot
(`set_warm_observer`, `:95`); it contains no database client at all.

**4. Therefore the 403 will not break cache warming at runtime.** The feature's API surface
(`GET/PUT /v1/warming`, `DELETE /v1/warming/{provider}` at `api/server.py:3017`, `3027`,
`3072`, and `_hosted_warm_observe` at `:4829`) goes through `_store`, which goes through the
RPCs, which are granted. Only an out-of-band tool — psql-as-service-role, a raw
`curl .../rest/v1/warm_prefixes`, or the Supabase Table Editor — sees the 403, and that is
the intended blast-radius reduction for a table holding KMS-encrypted provider credentials.

### What remains genuinely open

Two things this analysis cannot settle from the repository alone, both worth confirming
before declaring warming production-ready:

1. **Are the RPC `grant execute`s actually live on `wyfz`?** The `revoke`s clearly are (the
   403 proves it). Confirm the other half by calling a read-only RPC with the service-role
   key and expecting `200` rather than `404`/`403`:
   `POST /rest/v1/rpc/warm_read_status` with `{"p_organization_id": "<uuid>"}`. A `404
   PGRST202` would mean the function is missing or not exposed and warming *would* be
   broken — a different failure from the one reported here.
2. **Observability cost.** With no direct `SELECT`, routine operational inspection of warming
   state has exactly one door: `warm_read_status`. If operators need ad-hoc queries (backfill
   audits, budget forensics), the right move is an additional narrowly-scoped
   `security definer` reader function — **not** a table grant, which the §7.2 contract
   would reject.

**No SQL was written or applied as part of this runbook.**

---

## Appendix — quick reference

| service | Railway config | healthcheck | window | replicas | build |
| --- | --- | --- | --- | --- | --- |
| `compressor-production` | `deploy/railway.json` | `/ready` | 5m0s | 1 | `services/compress/Dockerfile` (torch layer cached) |
| `worker-production` | `deploy/railway-worker.json` | `/ready` | 2m0s | 2 | root `Dockerfile` |
| `Brevitas-Systems` | `railway.toml` / `railway.json` | `/v1/health/ready` | 2m0s | 2 | root `Dockerfile` |

Read-only commands that work from any session:

```bash
railway deployment list -s <service> [--limit N] [--json]
railway logs <deployId> -s <service> -b --lines N   # build log — healthcheck transcript
railway logs <deployId> -s <service> -d --lines N   # deploy/runtime log
railway logs -s <service> --network --since <ISO> --until <ISO>
railway variables -s <service> [--json]
railway status
```
