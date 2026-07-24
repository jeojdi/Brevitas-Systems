#!/bin/zsh
# One-time production env build-out for the Railway "Brevitas Production" project.
# Run from the repo root: zsh scripts/release/production-env-setup.sh
# Secrets flow Vercel/GCP -> Railway via shell substitution; nothing is printed.
set -e

PROJECT=divine-camera-465917-j7
TMP_ENV=$(mktemp)

echo "==> 1/5 Pulling production Vercel env (Supabase credentials)"
vercel env pull --yes --environment=production "$TMP_ENV" > /dev/null

getvar() { grep "^$1=" "$TMP_ENV" | head -1 | cut -d'"' -f2; }

echo "==> 2/5 Setting Supabase credentials on Railway API service"
railway variables --service Brevitas-Systems \
  --set "SUPABASE_URL=$(getvar NEXT_PUBLIC_SUPABASE_URL)" \
  --set "SUPABASE_SERVICE_ROLE_KEY=$(getvar SUPABASE_SERVICE_ROLE_KEY)" \
  --set "SUPABASE_ANON_KEY=$(getvar NEXT_PUBLIC_SUPABASE_ANON_KEY)" \
  --skip-deploys
rm -f "$TMP_ENV"

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
  --set "GCP_SA_KEY_JSON=$(cat "$SA_KEY")" \
  --skip-deploys
rm -f "$SA_KEY"

echo "DONE. Next: set the start command in the Railway dashboard (see checklist), then deploy."
