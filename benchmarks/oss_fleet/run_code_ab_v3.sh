#!/bin/bash
# codebase-scenario A/B with b9 v3 fixes + cache-isolation nonce
set -e
NONCE=$(date +%s)
echo "nonce=$NONCE"
for ARM in baseline brevitas; do
  METER=/tmp/oss-ab/code2_${ARM}.jsonl
  rm -f "$METER"
  cd /tmp/brev-wave-a
  if [ "$ARM" = "baseline" ]; then
    BREVITAS_PASSTHROUGH=1 BREVITAS_METER_FILE=$METER \
      /tmp/pip-smoke/bin/python -m uvicorn brevitas.proxy:proxy_app \
      --host 127.0.0.1 --port 4242 --log-level warning & PROXY=$!
  else
    BREVITAS_PASSTHROUGH=0 BREVITAS_AUTO_SHARED_PREFIX=1 BREVITAS_METER_FILE=$METER \
      /tmp/pip-smoke/bin/python -m uvicorn brevitas.proxy:proxy_app \
      --host 127.0.0.1 --port 4242 --log-level warning & PROXY=$!
  fi
  sleep 4
  cd /tmp/oss-ab
  /tmp/pip-smoke/bin/python autogen_analysis_ab.py --scenario codebase --arm $ARM \
    --rounds 8 --nonce $NONCE > /tmp/oss-ab/code2_${ARM}.log 2>&1 || echo "ARM $ARM FAILED (see log)"
  kill $PROXY 2>/dev/null; wait $PROXY 2>/dev/null || true
  echo "arm $ARM done: $(wc -l < $METER 2>/dev/null || echo 0) metered calls"
done
echo ALL_DONE
