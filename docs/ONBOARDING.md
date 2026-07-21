# Account and company onboarding

Brevitas uses one identity model for every account. A personal workspace is a
one-person company workspace; it can become a team workspace later without moving
projects, usage, keys, or billing data.

## Individual

1. Create an account at `/signup` and confirm the email address.
2. Choose **Personal workspace** and optionally name it.
3. The signed-in browser session can authorize BVX during onboarding. A short-lived
   dashboard credential is not minted until the server verifies onboarding evidence.
4. Install the released BVX manager and run `bvx install`. The interactive installer
   authenticates through the dashboard, configures an approved local AI tool, starts the
   local services, and performs its setup checks.
5. Run `bvx doctor` and require every installation diagnostic to pass. Send one normal
   prompt from a tool BVX reported as configured, then run `bvx stats`. Onboarding is
   verified only when **Requests proxied** increases; login or a healthy process alone is
   not sufficient.
6. Configure billing when ready.
7. Open **Company** later to invite teammates.

The dashboard does not accept a browser checkbox as proof of setup. The API keeps the
workspace pending until it has both a receipt-bound BVX device registration and a later
server-authoritative proxy request from that exact device key. Reloading the page cannot
skip this gate. This proves the credential and request path were connected; it does not
cryptographically attest that the executable was an official BVX release. Validate the
separately released CLI and its checksums as part of release onboarding.

## New company

1. The first user creates an account and chooses **Company workspace**.
2. They enter the company name and become `company_owner`.
3. In **Company**, they invite people and choose the least-privileged role:
   - `member`: shared workspace and roster access.
   - `company_admin`: member and service-account administration.
   - `billing_admin`: billing and administration-audit access.
4. An owner or company admin creates a scoped, expiring service account for each
   production environment. Human dashboard credentials are not production keys.

## Joining an existing company

1. An owner or company admin enters the person's exact email address in **Company**.
2. Brevitas displays a private invitation link once. Email delivery is not automated;
   the administrator sends that link to the invitee through a trusted channel.
3. The invitee opens the link and signs in with the exact confirmed email address that
   was invited. The secret stays in memory and is removed from the browser address bar.
4. After acceptance, Brevitas selects the joined company and creates a new short-lived
   dashboard credential for it.
5. A person who belongs to multiple companies can switch from the dashboard header.
   Every switch is checked against their current active membership and rotates the
   dashboard credential.

Invitations expire, are single-use, and cannot overwrite an existing membership in the
target company. Disabling or removing a member prevents that membership from becoming
active. The final active owner cannot be disabled, removed, or demoted.

## A company's end customers

Do not invite SaaS customers as Brevitas dashboard members. The company backend holds
one Brevitas service key per environment and sends its own stable customer identifier as
`X-Brevitas-Customer-ID`. Existing customers can be imported with `bvx onboard`; new
customers can be created automatically on first traffic. End customers never receive a
Brevitas service key.

## Deployment checklist

1. Apply all `supabase/migrations/` files in timestamp order, including active-company
   selection migration `202607170013_active_company_selection.sql` and durable onboarding
   migration `202607200016_durable_onboarding.sql`.
2. Configure Supabase email confirmation and allow the production `/invite` and
   `/email-confirmed` redirect URLs.
3. Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `COMPANY_ADMIN_CURSOR_SECRET`, and
   `COMPANY_ADMIN_INVITEE_PEPPER` on every API replica. The two company secrets must be
   different random values of at least 32 characters and consistent within an environment.
4. Set `API_URL`, `NEXT_PUBLIC_SUPABASE_URL`, and `NEXT_PUBLIC_SUPABASE_ANON_KEY` on the
   Next.js deployment. Never expose the service-role key or company secrets through a
   public environment variable.
5. Build the dashboard, deploy the API, and verify personal creation, receipt-bound BVX
   registration, same-key proxy evidence, reload persistence, exact-email invite
   acceptance, wrong-account denial, multi-company switching, member disable/removal,
   service-key rotation, and billing authorization in staging.
