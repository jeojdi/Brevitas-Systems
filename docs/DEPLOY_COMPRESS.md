# Private Railway compressor deployment

The production compressor is a private Railway service. It has no generated domain, no custom
domain, and no direct browser traffic. Only the Railway API and worker services call it over
Railway's isolated private network, using a shared internal bearer token.

This is a repository template and operator checklist. It does not provision Railway resources or
change a live environment.

## Service configuration

Create a Railway service from this repository with these settings:

- Config file path: `/deploy/railway.json`
- Dockerfile: `services/compress/Dockerfile`
- Region: the same primary US region as the API, worker, Supabase, and Redis Cloud
- Memory: at least 2 GiB (raise to 4 GiB if model startup is memory constrained)
- CPU: at least 1 vCPU
- Public networking: disabled; do not generate or attach a domain
- Private networking: enabled
- Readiness path: `/ready`

The image starts one Uvicorn process. Scale Railway replicas rather than starting multiple model
processes in one container; every process would load its own model and memory footprint.

Set these environment-specific managed secrets and bounded runtime values on the compressor:

```text
BREVITAS_COMPRESS_TOKEN=<high-entropy internal service token>
BREVITAS_COMPRESS_CONCURRENCY=2
BREVITAS_COMPRESS_ADMISSION_TIMEOUT=0.25
BREVITAS_COMPRESS_INFERENCE_TIMEOUT=120
BREVITAS_COMPRESS_MAX_PROMPT_CHARS=1000000
BREVITAS_COMPRESS_MAX_FORCE_TOKENS=128
```

Set the same token and the private URL on API and worker services:

```text
BREVITAS_COMPRESS_URL=http://compressor.railway.internal:8080
BREVITAS_COMPRESS_TOKEN=<same managed secret>
BREVITAS_COMPRESS_REQUIRED=false
BREVITAS_COMPRESS_PROBE_TIMEOUT_SECONDS=1
BREVITAS_COMPRESS_PROBE_WAIT_SECONDS=2.25
```

Replace `compressor` if the Railway service has another name. Internal HTTP is intentional:
Railway private traffic stays on its isolated encrypted network. The API rejects a production
compressor URL that is not under `.railway.internal`, and rejects any configured compressor that
lacks the internal token, before startup completes. If compression is optional, omit both URL and
token rather than configuring an unsafe or incomplete endpoint.

## Probe contract

The three unauthenticated probe endpoints contain booleans/status only and never echo tokens,
prompts, private hostnames, or provider data:

- `GET /live`: the event loop can answer. Dependency/model failure does not cause restart storms.
- `GET /startup`: the model load attempt finished. This distinguishes a slow image start.
- `GET /ready` (and compatibility alias `/health`): the model is loaded and the replica accepts
  inference traffic. Railway routes/promotes only when this returns `200`.

`POST /v1/optimize` always requires `Authorization: Bearer <token>`. Missing configuration fails
service startup; missing, malformed, or incorrect credentials return a contained error. Requests
have body, prompt, token-list, concurrency, admission-wait, and inference-time bounds.

## Safe rollout and shutdown

1. Deploy to staging without a public domain.
2. Wait for `/startup` and `/ready` to pass in Railway.
3. From the API service shell, call the private `/ready` endpoint and one authenticated synthetic
   optimization that contains no customer data.
4. Confirm direct internet access is impossible and missing/incorrect bearer tokens fail.
5. Roll the API after setting its private URL/token, then verify `/v1/health/ready` reports the
   compressor ready without returning its URL. The probe is cached, single-flight, bounded, and
   off the event loop.
6. Send SIGTERM in staging. Uvicorn stops accepting work, drains active requests for up to 180
   seconds, and releases the model during lifespan shutdown.

Never test with customer prompts. Do not run a live load test during repository preparation.

## Failure behavior

| Condition | Expected behavior |
| --- | --- |
| Model still loading | `/live` stays `200`; `/ready` is unavailable/not routable |
| Model load failed | `/startup` shows the attempt completed; `/ready` remains `503` |
| Missing internal token | Compressor startup fails closed |
| Unsafe/incomplete API compressor config | Production API startup fails before any probe or optimization request |
| Wrong/missing request token | Optimize request returns `401` or `403` |
| Capacity exhausted | Request returns `429` with a bounded retry hint |
| Inference exceeds deadline | Request returns `504`; its slot releases when the thread finishes |
| Optional compressor unavailable | API readiness stays `200` with `status=degraded`; alert on the compressor dependency |
| Required compressor unavailable | With `BREVITAS_COMPRESS_REQUIRED=true`, API readiness returns `503` |

Rollback by restoring the previous compressor image and confirming readiness before restoring API
traffic. Do not temporarily expose a public compressor endpoint as a workaround.
