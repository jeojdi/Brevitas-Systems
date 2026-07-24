# Release infrastructure preflight

The credential-free release preflight fails a release before tenant smoke tests when a public
hostname is unresolved, points at the wrong hosting platform, cannot complete verified HTTPS, or
does not implement the current API health contract. It never deploys, changes DNS, sends a body,
follows a redirect, or probes an operator-supplied URL.

The only accepted target profiles are:

| Target | Dashboard | API |
| --- | --- | --- |
| `staging` | `https://brevitas-systems-staging.vercel.app/` (Vercel) | `https://brevitas-api-staging-975273324573.us-west1.run.app` (Cloud Run) |
| `production` | `https://brevitassystems.com/` (Vercel) | `https://api.brevitassystems.com` (Railway) |

For each profile the gate resolves DNS and requires the documented Vercel, Cloud Run, or Railway
target,
then performs five read-only `GET` requests: the dashboard root, dashboard `/api/version`, API
`/v1/version`, `/v1/health/live`, and `/v1/health/ready`. Redirects fail. Normal certificate and
hostname verification are mandatory. The HTTPS responses must also carry the expected platform
routing signature. Staging uses Cloud Run's fixed deterministic service URL so the gate cannot
silently drift back to the retired Railway staging origin.

Both version endpoints must report the exact full commit SHA supplied in
`BREVITAS_EXPECTED_RELEASE_SHA`. Missing, abbreviated, conflicting, or different SHAs are hard
failures even when health is green. Build responses may contain only the validated commit SHA and
optional immutable build timestamp, release version, and `sha256:` image digest.

Those version fields are self-reported by the deployed applications. A match detects inconsistent
configuration and obvious deployment drift; it does not bind the served bytes or container to the
SHA, verify who built it, or provide cryptographic provenance. Even a matching `image_digest` is
only an asserted value until an independently signed registry artifact and workflow-identity
attestation are verified.

Readiness must report traffic acceptance, authoritative ready Postgres, coordination Redis, and
fresh successful active KMS evidence. Production additionally requires `status="ok"` and a ready
private compressor. Cloud Run staging may report `status="degraded"` only when the compressor is
explicitly `required=false` and is the sole unavailable dependency; every core dependency must
remain ready. KMS evidence is content-free and does not expose a key identifier, provider
response, ciphertext, or customer data. A legacy `/v1/health` response, `404`, incomplete JSON, or
any other degraded dependency shape is a hard failure and cannot satisfy this gate.

Run it manually from a trusted checkout:

```bash
BREVITAS_EXPECTED_RELEASE_SHA=FULL_TESTED_COMMIT_SHA npm run release:preflight -- staging
BREVITAS_EXPECTED_RELEASE_SHA=FULL_TESTED_COMMIT_SHA npm run release:preflight -- production
```

The same command is available through the **Release infrastructure preflight** manual GitHub
Actions workflow. The workflow runs only from `main` in the canonical non-fork repository and uses
the matching protected GitHub environment. It has read-only repository permission and requires no
application secret. It sets the expected comparison value to the workflow's immutable
`github.sha`; the deployed applications still self-report the values being compared. Unit tests
exercise failures with mocked DNS and HTTPS; pull-request CI never contacts live infrastructure.

After staging preflight succeeds, run the separately approval-gated staging smoke described in
`docs/RELEASE_SECURITY.md`. The release-security workflow uploads
`unsigned-ci-test-claim-<full-sha>` only after its build and test job succeeds on a canonical
`main` push. This self-declared JSON is unsigned and explicitly marks both
`cryptographic_attestation=false` and `deployment_verified=false`. It is a pointer to a GitHub run,
not independent proof that its statements or artifact bytes are authentic; retain and inspect the
run ID with the release record. Never
treat a successful production preflight as deployment approval, cryptographic attestation,
rollback evidence, or a substitute for the tenant smoke.

## Operator-owned actions

Repository code cannot create or repair public records. An operator must:

1. keep the fixed Vercel staging alias attached to the approved staging project and create the
   production dashboard record using Vercel's verified domain target;
2. keep the deterministic Cloud Run staging endpoint enabled and publicly invokable, and create
   the production API CNAME using the exact Railway-provided `*.up.railway.app` target;
3. verify public DNS, managed TLS, and platform ownership for every fixed endpoint;
4. configure required reviewers on the GitHub `staging` and `production` environments; and
5. run the staging preflight, approved staging smoke, and production preflight at the appropriate
   release stages;
6. verify Vercel and the target API platform both self-report the expected full SHA before
   promotion; and
7. if cryptographic provenance is required, sign the published image digest and generate a
   repository/workflow-identity attestation in the registry, then verify both under an approved
   keyless identity policy. Repository CI deliberately does not publish, sign, or deploy images.

Do not weaken the routing checks to make an unresolved or differently hosted domain pass. Change a
fixed hostname or provider signature only after an explicit infrastructure and domain-ownership
review.
