#!/bin/bash
set -u
PY=/tmp/pip-smoke/bin/python
LOGD=/tmp/oss-ab/v3logs; mkdir -p $LOGD
pkill -f "uvicorn brevitas.proxy" 2>/dev/null; sleep 1
rm -f /tmp/oss-ab/xrun_state.json
NONCE="xrun-$(date +%s)"
for RUN in run1 run2; do
  M=/tmp/oss-ab/v3_xrun_${RUN}.jsonl; rm -f "$M"
  cd /tmp/brev-wave-a
  BREVITAS_PASSTHROUGH=0 BREVITAS_STATE_FILE=/tmp/oss-ab/xrun_state.json BREVITAS_METER_FILE=$M \
    $PY -m uvicorn brevitas.proxy:proxy_app --host 127.0.0.1 --port 4242 --log-level warning \
    > $LOGD/xrun_proxy_${RUN}.log 2>&1 & PROXY=$!
  sleep 4
  cd /tmp/oss-ab
  $PY incremental_session_ab.py --arm xrun --nonce $NONCE --providers anthropic --cycles 2 \
    > $LOGD/xrun_${RUN}.log 2>&1 || echo "STAGE-FAIL xrun/$RUN"
  kill $PROXY 2>/dev/null; wait $PROXY 2>/dev/null || true
  echo "STAGE xrun/$RUN done ($(wc -l < "$M" 2>/dev/null || echo 0) calls)"
done
grep -h "cross-run state" $LOGD/xrun_proxy_run2.log || echo "NO-RESTORE-LOG"
echo "STAGE-XRUN-DONE"
