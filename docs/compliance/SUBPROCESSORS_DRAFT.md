# Subprocessor register and review template

**DRAFT — PRIVACY/LEGAL REVIEW REQUIRED — NOT PUBLISHED — LOCATIONS/TERMS MUST BE VERIFIED**

This is a diligence register, not notice that every vendor is enabled for every tenant. Before
production, owners must verify the contracting entity, purpose, data categories, locations,
security report, DPA, transfer mechanism, retention/deletion, incident terms, and current service.

| Provider/service | Proposed purpose | Data boundary | Region/transfer | Status |
| --- | --- | --- | --- | --- |
| Vercel | Website/dashboard/auth UI, checkout and webhook compute | Account/admin and billing routing data; no stored payment-card data | Initial US; verify | Review required |
| Railway | Public FastAPI, workers, private compressor | Encrypted queued content only within retention; operational metadata | Same primary US region; verify | Review required |
| Supabase | Postgres/Auth, PITR | Account, tenant configuration, encrypted queued/cache data, metadata, ledgers | Same primary US region; verify | Review required |
| Redis Cloud | Non-authoritative coordination/cache | Bounded opaque coordination state; semantic content only if enabled/encrypted | Same primary US region; verify | Review required |
| Stripe | Checkout, invoices, payment processing | Billing identity and processor tokens; Brevitas does not store card data | Verify | Review required |
| OpenAI | Customer-selected model processing | Transient request/response content and customer/provider credentials as configured | Contract/region dependent | Review/contract required |
| Anthropic | Customer-selected model processing | Transient request/response content and customer/provider credentials as configured | Contract/region dependent | Review/contract required |
| Other model provider | Customer-selected model processing | Must be separately approved before enablement | TBD | Not approved by this draft |
| Monitoring provider | OpenTelemetry logs/metrics/traces | Content-free allowlisted telemetry only | TBD | Provider not selected |
| Backup object store/KMS | Encrypted logical backup and key control | Encrypted database artifact, manifest, content-free evidence | TBD, separate failure domain | Provider not selected |

No names, emails, prompts, or responses enter the monitoring provider. Provider credentials are
managed secrets/KMS-protected and do not enter backup manifests, audit details, or logs.

## Change control

Security/privacy completes diligence and records residual risk; legal executes required DPA/SCC
terms; engineering validates data minimization and deletion; the owner obtains approval; then the
customer receives the contractually agreed advance notice (proposed 30 days, counsel to finalize).
Track objections, alternatives, effective date, and evidence. Emergency replacement still requires
prompt notice and retrospective review under the executed DPA.

Support EU Standard Contractual Clauses and an EU deployment only when an enterprise contract
requires them and the SCC annexes/transfer assessment/topology are complete. Keep this register and
the public version synchronized, but do not publish placeholders or unverified claims.
