#!/bin/bash
set -u
PY=/tmp/pip-smoke/bin/python
LOGD=/tmp/oss-ab/v3logs
pkill -f "uvicorn brevitas.proxy" 2>/dev/null; sleep 1
NONCE=$(date +%s)
for ARM in solo grouped; do
  [ "$ARM" = "grouped" ] && BG=1 || BG=0
  M=/tmp/oss-ab/v3_burst_${ARM}.jsonl; rm -f "$M"
  cd /tmp/brev-wave-a
  BREVITAS_PASSTHROUGH=0 BREVITAS_BATCH_GROUP=$BG BREVITAS_METER_FILE=$M \
    $PY -m uvicorn brevitas.proxy:proxy_app --host 127.0.0.1 --port 4242 --log-level warning \
    > $LOGD/burst_proxy_${ARM}.log 2>&1 & PROXY=$!
  sleep 4
  cd /tmp/oss-ab
  $PY burst_ab.py $ARM $NONCE-$ARM openai 2>&1 | tee $LOGD/burst_${ARM}.log | tail -8
  kill $PROXY 2>/dev/null; wait $PROXY 2>/dev/null || true
  echo "STAGE burst/$ARM done"
done
echo STAGE-BURST-DONE
