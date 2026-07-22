# Cloud Run staging cutover

The staging API runs as a Cloud Run service. The continuously polling job worker runs as a
Cloud Run Worker Pool, which has no public URL and does not scale to zero. Both attach
`brevitas-staging-runtime@divine-camera-465917-j7.iam.gserviceaccount.com`; never set
`GOOGLE_APPLICATION_CREDENTIALS` or create a service-account key.

Use `us-west1` for Cloud Run and Artifact Registry. Redis is in AWS `us-west-2`, and the
staging KMS key is the global `brevitas-staging/credential-envelope` key. The API and worker
manifests are `deploy/cloud-run-api-staging.yaml` and
`deploy/cloud-run-worker-staging.yaml`. Replace both `REPLACE_WITH_COMMIT_SHA` values with the
same full commit SHA that passed release CI, and deploy the resulting image by immutable digest
for the promoted revision.

## Bootstrap resources

1. Enable Cloud Run, Cloud Build, Artifact Registry, Secret Manager, IAM Credentials, Cloud KMS,
   and Compute Engine APIs.
2. Create the `brevitas-staging` Docker repository in Artifact Registry, region `us-west1`.
3. Create these Secret Manager resources and add version `1` without printing values:
   - `brevitas-staging-supabase-url`
   - `brevitas-staging-supabase-service-role-key`
   - `brevitas-staging-redis-url`
   - `brevitas-staging-company-admin-cursor-secret`
   - `brevitas-staging-company-admin-invitee-pepper`
4. Grant the runtime service account `roles/secretmanager.secretAccessor` on only those five
   secrets. Keep its existing KMS encrypt/decrypt grant scoped to the staging CryptoKey.
5. Route both workloads through Direct VPC egress using `brevitas-staging-vpc` and the
   `brevitas-staging-run-us-west1` (`10.42.0.0/24`) subnet. The
   `brevitas-staging-nat-us-west1` Cloud NAT uses the reserved
   `brevitas-staging-egress-us-west1` address, `34.105.116.148`.
6. Build the root `Dockerfile` with the checked-in `deploy/cloudbuild-api.yaml` configuration and the
   full source commit (for example, `gcloud builds submit --config=deploy/cloudbuild-api.yaml
   --substitutions=_BREVITAS_BUILD_SHA=$COMMIT_SHA .`). Replace both manifest placeholders,
   deploy the API service and Worker Pool, and then make only the API service publicly invokable.
7. Keep `BREVITAS_BILLING_ENABLED=false` and the worker role `nonbilling` until the Stripe and
   Supabase gates below pass.

## Supabase inputs and gates

Required inputs:

- The staging project URL and a staging-only service-role key.
- A separate browser-safe publishable/anon key for Vercel; it never belongs in the API or worker
  secret set.
- The Supavisor transaction-pooler database URL for maintenance tooling, not application request
  configuration.
- Separate staging values for the company cursor-signing secret and invitee pepper, each at least
  32 characters.

Required gates:

- Apply the ordered release migration manifest through
  `202607200018_workspace_experiences.sql`. Run `202607200004` through `202607200006` only with
  the guarded billing-maintenance procedure in `docs/STRIPE_BILLING.md`.
- Confirm service-owned tables have RLS enabled with no end-user policies and that only
  `service_role` can use their tables/RPCs.
- Enable PITR and verify one restore drill before promoting production.
- Confirm `/v1/health/ready` reports Postgres ready; never accept SQLite fallback in Cloud Run.

## Stripe inputs and gates

Keep Stripe in test mode for staging. Required inputs are:

- `STRIPE_SECRET_KEY` (`sk_test_...`) for the worker and Vercel server routes.
- `STRIPE_WEBHOOK_SECRET` (`whsec_...`) for Vercel only.
- `STRIPE_PRICE_ID` for the active USD weekly per-unit metered Price.
- `STRIPE_METER_EVENT_NAME`, normally `brevitas_fee_microusd`.
- `BREVITAS_BILLING_WEEKLY_CAP_USD` and the manual `BILLING_RECOVERY_SECRET`.
- `BREVITAS_PUBLIC_URL` for the staging Vercel origin; keep automatic tax disabled until tax
  registration and product tax behavior are reviewed.

Required gates:

1. Run `STRIPE_SECRET_KEY=sk_test_... npm run billing:setup` once and record the resulting Price
   ID and meter event name in the secret store.
2. Configure the Vercel webhook for `checkout.session.completed`,
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.paid`, and `invoice.payment_failed`.
3. Confirm the Price is active, USD, weekly, per-unit, metered, attached to an active `sum` meter,
   and uses the configured customer/value mappings.
4. Run the staging canary through Checkout, subscription activation, one metered event, invoice
   reconciliation, duplicate delivery, and payment failure.
5. Prove the Cloud Run worker is the sole meter-event writer before setting
   `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER=true`, changing its role to `authoritative`, and
   enabling billing.

## Redis rotation and network cutover

Do not disable the Redis default user before the Cloud Run revision has a tested replacement
credential. Create a named staging ACL user with only the data permissions the application needs,
store its TLS URL as a new Secret Manager version, deploy and verify API/worker readiness, and
then disable the default user. CIDR allow-listing requires a stable Cloud Run egress address
(Direct VPC egress plus Cloud NAT). The staging CIDR is `34.105.116.148/32`; add only that CIDR
after the replacement user is live and the egress path is verified, or the cutover will lock out
every instance.

## Smoke checks

- `GET /v1/health/live` returns `200`.
- `GET /v1/health/ready` returns `200`, with Postgres, Redis, and KMS all ready. An omitted
  optional compressor may make the payload degraded but must not make core readiness pass when
  any authoritative dependency is unavailable.
- `GET /v1/version` reports exactly the promoted commit.
- The Worker Pool logs one successful KMS/Redis/Postgres dependency cycle and accepts jobs; it
  remains nonbilling until the Stripe gate is completed.
