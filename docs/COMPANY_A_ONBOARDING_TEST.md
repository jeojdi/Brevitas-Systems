# Company A onboarding test

This workflow proves the intended enterprise identity model:

1. A signed-in Company A administrator creates one production service key.
2. Company A bulk-imports 100 existing customers using exact IDs from its database.
3. Those 100 customers submit AI jobs through Company A's backend.
4. Fifty customers who were not imported submit their first AI jobs and are created automatically.
5. Re-imports and request retries remain idempotent.
6. The final inventory contains 150 customers, 150 customer-scoped jobs, and one organization service key.
7. One customer cannot retrieve another customer's job.

Run the visible workflow:

```bash
.venv/bin/pytest -q -s tests/test_company_a_workflow.py
```

Expected result:

```text
Company A workflow passed: 100 existing customers imported, 50 new customers auto-onboarded, 150 isolated jobs, 1 service key.
1 passed
```

This is a deterministic local contract test using SQLite for durable state. The production launch gate remains the equivalent workflow against staging Postgres and Redis.

## BVX onboarding command

The companion BVX repository provides the interactive workflow:

```bash
bvx onboard
```

It asks for the Company A backend directory and a past-customer export, scans the codebase with AgentMap, detects unambiguous customer-specific ID fields, and shows a dry run. Apply it with:

```bash
bvx onboard --customers ./past-customers.json --apply /path/to/company-backend
```

Supported inputs include comma/semicolon/tab-delimited exports, JSON arrays and wrappers, JSONL/NDJSON, nested schemas, and keyed JSON maps. Generic or ambiguous IDs require `--id-field`; names remain local unless Company A explicitly opts in with `--name-field`. Direct database/workbook access is deliberately disabled so database credentials and unrelated columns never enter BVX.
