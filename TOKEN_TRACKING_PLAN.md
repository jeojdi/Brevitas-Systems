# Per-Pipeline / Per-Agent Token-Savings Tracking + Billing + CI

**Goal:** track exactly *how much Brevitas saved*, sliced by **service → pipeline → agent → run → call**, surface it on the dashboard, and bill a % of verified savings per slice. Prove it end-to-end with a real **DeepSeek-backed marketing-agency** multi-agent backend wired into CI.

**Decisions (locked):**
- Datastore: **SQLite primary** (`api/store.py`) extended with new columns, **mirrored to Supabase** (`billing_events`).
- Billing: compute `brevitas_fee` (% of *quality-verified* savings) + **invoice preview with line items by pipeline/agent**. No Stripe this round.
- Test backend: **real DeepSeek**, one distinct agent per role. CI required checks run a **deterministic mock provider**; a **separate opt-in job** runs the real DeepSeek agency behind a `DEEPSEEK_API_KEY` secret.

---

## 1. The gap (from codebase scan)

| Layer | File | Today | Needed |
|---|---|---|---|
| Tracking store | `api/store.py` `usage_log` | keyed by `key_hash` + `session_id` + provider/model | add `pipeline`, `agent`, `run_id` + per-slice aggregations |
| Ingest (SDK) | `brevitas/wrappers/*.py`, `brevitas/_compress.py` | sends provider/model/baseline/compressed/session_id | carry pipeline/agent/run_id labels |
| Ingest (proxy) | `brevitas/proxy.py` | one session per provider-key | read `x-brevitas-pipeline/agent/run-id` headers |
| Label source | `brevitas/session.py` | random `session_id` only | contextvar-based labels + per-call override |
| API | `api/server.py` `/v1/usage`, `/v1/compress`, `/v1/stats` | flat per-key stats | accept labels; add per-pipeline/agent/run stats + filters |
| Dashboard | `dashboard/src/components/*` | Overview + Billing per-key | Pipelines tab w/ drilldown + invoice line items |
| Supabase | `supabase/migrations/20260626_create_billing.sql` | `billing_events` per user | add label columns + `savings_by_pipeline/agent` views |
| CI | `.github/workflows/security.yml` only | security scan | add test + lint + build + DeepSeek-agency jobs |

**Attribution model.** A *call* = one LLM request. Calls roll up to an *agent* (named node, e.g. `copywriter`), agents roll up to a *pipeline* (named workflow, e.g. `campaign-launch`), a *run* = one execution of a pipeline (a trace, `run_id`). Each call records `baseline_tokens` (no-Brevitas cost) vs `optimized_tokens` (actual), so savings = baseline − actual, attributed to its labels. Tracking stays **decoupled from the savings mechanism** (compression today, native caching per `REVAMP_PLAN.md`): it only records baseline vs actual + labels.

---

## Phase A — Data model + label propagation (foundation)

**A1. Store columns (`api/store.py`).** Additively migrate `usage_log` (reuse the existing `PRAGMA table_info` migration block):
- `pipeline TEXT NOT NULL DEFAULT ''`
- `agent TEXT NOT NULL DEFAULT ''`
- `run_id TEXT NOT NULL DEFAULT ''`
Update `record_usage(...)` signature + INSERT to accept/persist them.

**A2. Label propagation in the SDK (`brevitas/`).** New `brevitas/labels.py` using `contextvars.ContextVar`:
```python
brevitas.start_run(pipeline="campaign-launch")        # sets run_id + pipeline
with brevitas.agent("copywriter"):                    # context manager
    client.messages.create(...)                       # auto-tagged
client.messages.create(..., _brevitas_meta={"agent": "editor"})  # per-call override
```
- Resolution order: per-call `_brevitas_meta` > contextvar > config default > `""`.
- Thread/async safe (contextvars copy into worker threads explicitly where the proxy spawns them).

**A3. Plumb labels through ingest.**
- `brevitas/_compress.py`: `report_usage(...)` and `/v1/compress` calls include `pipeline/agent/run_id` resolved from labels.
- `brevitas/wrappers/anthropic.py` + `openai.py`: read labels, pass to `report_usage`.
- `brevitas/proxy.py`: parse `x-brevitas-pipeline`, `x-brevitas-agent`, `x-brevitas-run-id` headers; thread them into `report_usage`. (Headers = zero-code path for any framework.)

**A4. API accepts labels (`api/server.py`).**
- `UsageReportRequest` + `CompressRequest`: add optional `pipeline`, `agent`, `run_id`.
- Pass to `_store.record_usage(...)`.

**Acceptance A:** a call made under `start_run` + `agent("copywriter")` writes a `usage_log` row with correct `pipeline/agent/run_id`; old callers (no labels) still work (defaults to `''`).

---

## Phase B — Aggregation + stats API

**B1. Store queries (`api/store.py`).** New methods, all filterable by `start`,`end`,`pipeline`,`agent`:
- `get_stats_by_pipeline(key_hash, ...)` → per pipeline: calls, tokens_saved, savings_pct, avg_quality, cost_saved, fee.
- `get_stats_by_agent(key_hash, pipeline=None, ...)` → per agent (optionally within a pipeline).
- `get_stats_by_run(key_hash, pipeline=None, ...)` → per run trace (timeline).
- Extend `get_stats` to also return `by_pipeline` + `by_agent` arrays.

**B2. Endpoints (`api/server.py`).**
- `GET /v1/stats/pipelines`
- `GET /v1/stats/agents?pipeline=`
- `GET /v1/stats/runs?pipeline=`
- All accept `?start=&end=&pipeline=&agent=`. Rate-limited like `/v1/stats`.

**Acceptance B:** sum of per-agent savings within a pipeline == that pipeline's total; sum of pipeline savings == account total (reconciliation test).

---

## Phase C — Dashboard: Pipelines view + invoice line items

**C1. New `Pipelines` tab (`dashboard/src/components/Pipelines.jsx`), added to `TABS` in `App.jsx`.**
- Top: bar chart "Savings $ by pipeline" (recharts, already a dep).
- Click a pipeline → drilldown: per-agent table (calls, tokens saved, savings %, quality, $ saved, fee) + per-agent bar.
- Recent runs table (run_id, pipeline, started, savings %, $) → click a run for its calls.

**C2. Billing tab (`dashboard/src/components/Billing.jsx`).**
- Weekly invoice preview: line items grouped by pipeline (then agent), each showing verified savings $ and the 25% fee, plus invoice total. Pulls `/v1/stats/pipelines` + `billing_by_week`.

**Acceptance C:** dashboard shows savings broken down by pipeline and agent; invoice preview line items sum to the weekly fee total.

---

## Phase D — DeepSeek marketing-agency backend (the real multi-agent workload)

**`examples/marketing_agency/`** — a realistic campaign pipeline, one **distinct DeepSeek agent per role**, all calls routed through Brevitas (so every call is tracked + attributed):

| Agent | Role | DeepSeek model |
|---|---|---|
| `intake` | parse the client brief into structured goals | `deepseek-chat` |
| `researcher` | market/competitor/audience research | `deepseek-reasoner` |
| `strategist` | channel + messaging strategy | `deepseek-reasoner` |
| `copywriter` | ad/email/social copy variants | `deepseek-chat` |
| `seo_optimizer` | keywords + on-page suggestions | `deepseek-chat` |
| `editor` | brand/QA pass over copy | `deepseek-chat` |
| `reporter` | assemble final campaign brief | `deepseek-chat` |

- **Orchestrator** (`orchestrator.py`): sequential DAG (intake → researcher → strategist → {copywriter, seo_optimizer} → editor → reporter). Each agent: `with brevitas.agent("<role>"):` around its DeepSeek call. `start_run(pipeline="campaign-launch")` per campaign → one `run_id` per execution. Prior agents' outputs flow as context (exercises cross-hop savings + DeepSeek prefix caching).
- **Provider abstraction** (`provider.py`): `BREVITAS_AGENCY_PROVIDER=mock|deepseek`.
  - `mock`: deterministic canned responses + realistic `usage` (no keys, for CI).
  - `deepseek`: real calls via Brevitas proxy/SDK (`base_url=https://api.deepseek.com`, `DEEPSEEK_API_KEY`).
- **`run.py`**: runs one campaign from a sample brief, then prints the per-agent savings table and writes the run's `run_id`.
- `examples/marketing_agency/README.md`: how to run mock vs real DeepSeek.

**Acceptance D:** `BREVITAS_AGENCY_PROVIDER=deepseek python -m examples.marketing_agency.run` completes a campaign; `/v1/stats/agents?pipeline=campaign-launch` shows all 7 agents with savings + quality; totals reconcile.

---

## Phase E — CI/CD

**`.github/workflows/ci.yml`** (new; existing `security.yml` stays):
- `lint` — `ruff` + `black --check` (Python), `eslint` (JS).
- `python-tests` — matrix py3.11/3.12; install `api/requirements.txt` + test deps; `pytest -q --cov`. Includes the **integration test that runs the marketing agency against the mock provider** through FastAPI `TestClient`, asserting per-agent/per-pipeline rows + reconciliation.
- `frontend-build` — `npm ci` + `next build`; `cd dashboard && npm ci && npm run build`.
- `deepseek-agency` — **opt-in**, `if: ${{ secrets.DEEPSEEK_API_KEY != '' }}`; runs the real DeepSeek agency end-to-end and asserts savings recorded. Not a required check (kept off the merge gate so PRs never block on provider flakiness/cost).

Mark `lint`, `python-tests`, `frontend-build` as the **required** status checks. Optionally extend `.husky` pre-push to run `pytest -q`.

**Acceptance E:** PRs run lint + tests + build automatically; green required checks; DeepSeek job runs only when the secret is present.

---

## Phase F — Supabase mirror

**New migration `supabase/migrations/<date>_add_tracking_labels.sql`:**
- `alter table billing_events add column pipeline text not null default ''`, same for `agent`, `run_id`.
- Views: `savings_by_pipeline` (user_id, pipeline, month, calls, tokens_saved, cost_saved, fee) and `savings_by_agent` (… + agent). RLS already scopes by `user_id`.
- Update the mirror writer to forward the new labels.

**Acceptance F:** Supabase rows carry labels; views return per-pipeline/agent rollups under RLS.

---

## Testing strategy (TDD, write tests first per phase)

- **Unit:** store migration + `record_usage` with labels; `get_stats_by_pipeline/agent/run`; label contextvar resolution + override precedence; proxy header parsing; per-provider cost math (DeepSeek rates already in `store.py`).
- **Integration:** full mock marketing-agency run → 7 agents recorded under one `run_id`, savings > 0, **reconciliation** (Σ agent = pipeline = account).
- **API:** `/v1/usage` + `/v1/compress` persist labels; stats endpoints honor `start/end/pipeline/agent` filters.
- **E2E (optional):** seed DB → dashboard Pipelines tab renders breakdown (Playwright via `/e2e`).

## Sequencing & risk
A → B → (C ∥ D) → E → F. A is the unlock (nothing attributes without labels). D depends on A/B for assertions. Lowest risk: additive SQLite migration mirrors the existing pattern; labels default to `''` so all current callers keep working. Biggest care: contextvar propagation across the proxy's worker threads (explicit `contextvars.copy_context()`), and keeping DeepSeek's auto prefix-cache intact (don't mutate the stable prefix — already handled in `_compress.py`).
