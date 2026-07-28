# Account and company onboarding

Brevitas uses one identity model for every account. A personal workspace is a
one-person company workspace; it can become a team workspace later without moving
projects, usage, keys, or billing data.

The selected experience is persisted on the workspace, not inferred from the login
URL. Personal workspaces get a focused **Projects / Connect / Workspace** view.
Enterprise workspaces get **Repositories / Connect / Team & keys / API Keys** so
human roles, local device credentials, and production service identities stay
visibly separate. Authorization remains membership-derived in both views.

## Individual

1. Create an account at `/signup` and confirm the email address.
2. Choose **Personal workspace** and optionally name it.
3. The signed-in browser session can authorize BVX during onboarding. A short-lived
   dashboard credential is not minted until the server verifies onboarding evidence.
4. Copy the operating-system quick-start command from **Connect**. On macOS and Linux
   it installs the released BVX manager and runs `bvx install` in one command. The installer
   authenticates through the dashboard, configures an approved local AI tool, starts the
   local services, and performs its setup checks.
5. Run `bvx doctor` and require every installation diagnostic to pass, then send one normal
   prompt from a configured tool. The website checks server evidence every three seconds
   and opens the dashboard automatically. `bvx stats` remains available as a terminal-side
   confirmation. Login or a healthy process alone is not sufficient.
6. Configure billing when ready.
7. Open **Workspace** later to invite teammates.

The dashboard does not accept a browser checkbox as proof of setup. The API keeps the
workspace pending until it has both a receipt-bound BVX device registration and a later
proxy receipt reported from that exact device key (the released CLI's local proxy reports
these over `/v1/usage`; the hosted proxy records them in-process). Reloading the page
cannot skip this gate. This proves the approved device credential was provisioned and used
to report proxy traffic; because the local proxy self-reports its receipts, it does not
prove a provider call occurred, nor cryptographically attest that the executable was an
official BVX release. Completion gates only the tenant's own dashboard experience. Validate
the separately released CLI and its checksums as part of release onboarding.

## New company

1. The first user creates an account and chooses **Company workspace**.
2. They enter the company name and become `company_owner`.
3. The enterprise view explains the key distinction during setup: `bvx install` creates
   a revocable key for that admin device; production systems never reuse it.
4. In **Team & keys**, they invite people and choose the least-privileged role:
   - `member`: shared workspace and roster access.
   - `company_admin`: member and service-account administration.
   - `billing_admin`: billing and administration-audit access.
5. An owner or company admin creates a scoped, expiring service account for each
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
   migration `202607200016_durable_onboarding.sql`, followed by workspace-experience
   migration `202607200018_workspace_experiences.sql`.
2. Configure Supabase email confirmation and allow the production `/invite`,
   `/email-confirmed`, and `/dashboard` redirect URLs, including the `?audience=personal` and
   `?audience=enterprise` variants the sign-in routes generate. Confirmation mail requires
   custom SMTP with a verified sender domain — see
   [the email template and delivery notes](../supabase/templates/README.md). Signup returns
   `500 unexpected_failure` until that is in place. Apply all of it to the project the deployed
   bundle actually names: `VITE_SUPABASE_URL` is inlined at build time, so a stale value routes
   production to a different project than the dashboard you are editing.
3. Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `COMPANY_ADMIN_CURSOR_SECRET`, and
   `COMPANY_ADMIN_INVITEE_PEPPER` on every API replica. The two company secrets must be
   different random values of at least 32 characters and consistent within an environment.
4. Set `BREVITAS_API_URL`, `NEXT_PUBLIC_SUPABASE_URL`, and `NEXT_PUBLIC_SUPABASE_ANON_KEY`
   on the Next.js deployment. (`API_URL` is a deprecated legacy fallback only.) Never expose
   the service-role key or company secrets through a public environment variable.
5. Build the dashboard, deploy the API, and verify personal creation, receipt-bound BVX
   registration, same-key proxy evidence, reload persistence, exact-email invite
   acceptance, wrong-account denial, multi-company switching, member disable/removal,
   service-key rotation, and billing authorization in staging.
