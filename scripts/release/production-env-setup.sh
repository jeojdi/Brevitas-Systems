#!/bin/zsh
# One-time production env build-out for the Railway "Brevitas Production" project.
# Run from the repo root: zsh scripts/release/production-env-setup.sh
# Secrets flow Vercel/GCP -> Railway via shell substitution; nothing is printed.
#
# Both temp files hold production secrets -- the Supabase service-role key, then a
# freshly minted GCP KMS service-account private key, which is the credential that
# decrypts every customer provider key -- so they are created under `umask 077` and
# removed by a trap instead of only on the happy path. Any abort under `set -e`
# previously left plaintext in /tmp indefinitely with no record it existed.
#
# Re-running is not free: step 4 mints a NEW service-account private key on every run
# (GCP caps a service account at 10) and steps 2/5 overwrite live Railway variables.
set -euo pipefail
umask 077

# One trap covers both files. SA_KEY is unset during the first phase, hence the
# ${VAR:-} guards, which are also what keep the trap itself from tripping `set -u`.
TMP_ENV=""
SA_KEY=""
trap 'rm -f "${TMP_ENV:-}" "${SA_KEY:-}"' EXIT INT TERM

PROJECT=divine-camera-465917-j7
TMP_ENV=$(mktemp)

echo "==> 1/5 Pulling production Vercel env (Supabase credentials)"
vercel env pull --yes --environment=production "$TMP_ENV" > /dev/null

# awk rather than `grep "^$1=" | head -1 | cut`: a single process cannot be
# SIGPIPE-killed mid-pipeline (which under `pipefail` would abort the script on a
# perfectly good lookup), it exits 0 whether or not the key exists, and it keeps any
# '=' inside the value intact. An absent or renamed key yields the empty string,
# which fail_missing turns into a hard stop instead of quietly running
# `--set "SUPABASE_SERVICE_ROLE_KEY="` and wiping the production API's database
# credential on the next deploy.
getvar() {
  awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); gsub(/^"|"$/, ""); print; exit }' "$TMP_ENV"
}

fail_missing() {
  echo "error: $1 is missing or empty in the production Vercel env" >&2
  echo "       refusing to overwrite the live Railway variable with an empty value" >&2
  exit 1
}

fail_shape() {
  echo "error: $1 does not have the expected shape; verify the Vercel variable" >&2
  exit 1
}

echo "==> 2/5 Setting Supabase credentials on Railway API service"
SUPABASE_URL_VALUE=$(getvar NEXT_PUBLIC_SUPABASE_URL)
SUPABASE_SERVICE_ROLE_VALUE=$(getvar SUPABASE_SERVICE_ROLE_KEY)
SUPABASE_ANON_VALUE=$(getvar NEXT_PUBLIC_SUPABASE_ANON_KEY)
[[ -n "$SUPABASE_URL_VALUE" ]] || fail_missing NEXT_PUBLIC_SUPABASE_URL
[[ -n "$SUPABASE_SERVICE_ROLE_VALUE" ]] || fail_missing SUPABASE_SERVICE_ROLE_KEY
[[ -n "$SUPABASE_ANON_VALUE" ]] || fail_missing NEXT_PUBLIC_SUPABASE_ANON_KEY
[[ "$SUPABASE_URL_VALUE" == https://*.supabase.co* ]] || fail_shape NEXT_PUBLIC_SUPABASE_URL
[[ "$SUPABASE_SERVICE_ROLE_VALUE" == (sb_secret_*|ey*.*.*) ]] \
  || fail_shape SUPABASE_SERVICE_ROLE_KEY
[[ "$SUPABASE_ANON_VALUE" == (sb_publishable_*|ey*.*.*) ]] \
  || fail_shape NEXT_PUBLIC_SUPABASE_ANON_KEY
railway variables --service Brevitas-Systems \
  --set "SUPABASE_URL=$SUPABASE_URL_VALUE" \
  --set "SUPABASE_SERVICE_ROLE_KEY=$SUPABASE_SERVICE_ROLE_VALUE" \
  --set "SUPABASE_ANON_KEY=$SUPABASE_ANON_VALUE" \
  --skip-deploys
rm -f "$TMP_ENV"
TMP_ENV=""

echo "==> 3/5 Creating production KMS keyring + key (idempotent)"
gcloud kms keyrings create brevitas-production --location=global --project=$PROJECT 2>/dev/null || true
gcloud kms keys create credential-envelope --location=global --keyring=brevitas-production \
  --purpose=encryption --project=$PROJECT 2>/dev/null || true

echo "==> 4/5 Service account + key (temporary exception per DEPLOYMENT_GUIDE; owner=you, review in 90d)"
gcloud iam service-accounts create brevitas-prod-kms \
  --display-name="Brevitas production KMS (Railway, temporary key-based)" \
  --project=$PROJECT 2>/dev/null || true
gcloud kms keys add-iam-policy-binding credential-envelope \
  --location=global --keyring=brevitas-production --project=$PROJECT \
  --member="serviceAccount:brevitas-prod-kms@${PROJECT}.iam.gserviceaccount.com" \
  --role=roles/cloudkms.cryptoKeyEncrypterDecrypter > /dev/null

SA_KEY=$(mktemp)
gcloud iam service-accounts keys create "$SA_KEY" \
  --iam-account="brevitas-prod-kms@${PROJECT}.iam.gserviceaccount.com" --project=$PROJECT

echo "==> 5/5 Setting KMS key id + SA credential on Railway API service"
railway variables --service Brevitas-Systems \
  --set "BREVITAS_KMS_KEY_ID=projects/${PROJECT}/locations/global/keyRings/brevitas-production/cryptoKeys/credential-envelope" \
  --skip-deploys

# `railway variables --set "K=V"` puts V in argv, which /proc/<pid>/cmdline and
# `ps auxww` expose to every process of this user and to root, so use the CLI's file
# form for the SA private key when it has one. Older releases do not; that fallback
# names the residual local exposure instead of hiding it. The durable fix is Workload
# Identity Federation, so no SA private key is downloaded at all -- see
# docs/CREDENTIAL_SECURITY.md.
#
# Keep this inline. zsh does not run the EXIT trap when `set -e` aborts inside a
# function, and wrapping the call in `if ! fn` suppresses err_exit for the whole
# function body, so either shape would leave the private key in /tmp on failure.
if railway variables --help 2>/dev/null | grep -q -- '--set-from-file'; then
  railway variables --service Brevitas-Systems \
    --set-from-file "GCP_SA_KEY_JSON=$SA_KEY" --skip-deploys
else
  echo "note: this railway CLI has no --set-from-file, so GCP_SA_KEY_JSON is briefly" >&2
  echo "      visible in this host's process list; run only on a single-operator box" >&2
  railway variables --service Brevitas-Systems \
    --set "GCP_SA_KEY_JSON=$(cat "$SA_KEY")" --skip-deploys
fi
rm -f "$SA_KEY"
SA_KEY=""

echo "DONE. Next: set the start command in the Railway dashboard (see checklist), then deploy."
