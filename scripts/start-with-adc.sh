#!/bin/sh
# Materialize Google Application Default Credentials from GCP_SA_KEY_JSON when the
# hosting platform (Railway) cannot issue a workload identity. This is the documented
# temporary exception in DEPLOYMENT_GUIDE.md; platforms with attached identities
# (Cloud Run) simply never set GCP_SA_KEY_JSON and this shim is a no-op.
set -eu
if [ -n "${GCP_SA_KEY_JSON:-}" ]; then
  umask 077
  printf '%s' "$GCP_SA_KEY_JSON" > /tmp/gcp-adc.json
  export GOOGLE_APPLICATION_CREDENTIALS=/tmp/gcp-adc.json
  unset GCP_SA_KEY_JSON
fi
exec "$@"
