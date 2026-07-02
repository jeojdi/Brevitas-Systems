#!/bin/bash
set -u
NONCE=$(date +%s)
PY=/tmp/pip-smoke/bin/python
LOGD=/tmp/oss-ab/v3logs; mkdir -p $LOGD
pkill -f "uvicorn brevitas.proxy" 2>/dev/null; sleep 1
echo "STAGE0 incremental nonce=$NONCE"
# brevitas FIRST: any provider-cache leak then favors baseline (savings = lower bound)
for ARM in brevitas baseline; do
  M=/tmp/oss-ab/v3_incr_${ARM}.jsonl; rm -f "$M"
  cd /tmp/brev-wave-a
  if [ "$ARM" = "baseline" ]; then
    BREVITAS_PASSTHROUGH=1 BREVITAS_METER_FILE=$M \
      $PY -m uvicorn brevitas.proxy:proxy_app --host 127.0.0.1 --port 4242 --log-level warning & PROXY=$!
  else
    BREVITAS_PASSTHROUGH=0 BREVITAS_AUTO_SHARED_PREFIX=1 BREVITAS_METER_FILE=$M \
      $PY -m uvicorn brevitas.proxy:proxy_app --host 127.0.0.1 --port 4242 --log-level warning & PROXY=$!
  fi
  sleep 4
  cd /tmp/oss-ab
  $PY incremental_session_ab.py --arm $ARM --nonce $NONCE > $LOGD/incr_${ARM}.log 2>&1 \
    || echo "STAGE-FAIL incr/$ARM (see $LOGD/incr_${ARM}.log)"
  kill $PROXY 2>/dev/null; wait $PROXY 2>/dev/null || true
  echo "STAGE incr/$ARM done ($(wc -l < "$M" 2>/dev/null || echo 0) metered calls)"
done
echo "STAGE-INCR-DONE"
