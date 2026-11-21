#!/usr/bin/env bash
# T4 no-loss chaos test (spec §19).
#
# During a 200 ev/s k6 load:
#   +FAIL_AT s  mockgw: SMS returns 503 for SMS_FAIL_SECONDS
#   +KILL_AT s  docker kill one worker container (only if > 1 worker runs; restarted afterwards)
# After the load: wait for the queues to drain, reconcile (see reconcile.py) and print `LOST = N`.
# Exit code 1 when N > 0.
#
# Env (defaults):
#   API_URL http://localhost:8080   MOCKGW_URL http://localhost:8090   RATE 200   DURATION 3m
#   LOAD_SECONDS 180 (must match DURATION)   FAIL_AT 30   KILL_AT 60   SMS_FAIL_SECONDS 60
#   COMPOSE_PROJECT gsalert   WORKER_SERVICE worker   FORCE_KILL_SINGLE 0
#   DRAIN_TIMEOUT 300   RUN_ID (generated)   RAW_DIR loadtest/results/raw
set -euo pipefail

cd "$(dirname "$0")/.."

export API_URL="${API_URL:-http://localhost:8080}"
MOCKGW_URL="${MOCKGW_URL:-http://localhost:8090}"
RATE="${RATE:-200}"
LOAD_SECONDS="${LOAD_SECONDS:-180}"
DURATION="${DURATION:-${LOAD_SECONDS}s}"
FAIL_AT="${FAIL_AT:-30}"
KILL_AT="${KILL_AT:-60}"
SMS_FAIL_SECONDS="${SMS_FAIL_SECONDS:-60}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-gsalert}"
WORKER_SERVICE="${WORKER_SERVICE:-worker}"
FORCE_KILL_SINGLE="${FORCE_KILL_SINGLE:-0}"
export RAW_DIR="${RAW_DIR:-loadtest/results/raw}"
export RUN_ID="${RUN_ID:-t4$(date +%s)}"
# Typed raises are excluded: whether every raise yields a new alert is not part of the no-loss claim.
export TYPED_RATIO="${TYPED_RATIO:-0}"
export RATE DURATION NAME=t4

mkdir -p "$RAW_DIR"

log() { printf '[t4 %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

worker_containers() {
  docker ps -q \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" \
    --filter "label=com.docker.compose.service=${WORKER_SERVICE}"
}

log "preflight: api, mockgw, rabbitmq management"
curl -fsS "${API_URL}/healthz" >/dev/null || { log "API not reachable at ${API_URL}"; exit 2; }
curl -fsS "${MOCKGW_URL}/admin/state" >/dev/null || { log "mockgw not reachable at ${MOCKGW_URL}"; exit 2; }

KILLED=0

chaos() {
  sleep "$FAIL_AT"
  log "+${FAIL_AT}s mockgw: SMS fails for ${SMS_FAIL_SECONDS}s"
  curl -fsS -X POST "${MOCKGW_URL}/admin/fail" -H 'Content-Type: application/json' \
    -d "{\"channel\":\"sms\",\"seconds\":${SMS_FAIL_SECONDS}}" || log "mockgw fail call failed"
  echo
  sleep $((KILL_AT - FAIL_AT))
  local workers count
  workers="$(worker_containers)"
  count="$(printf '%s\n' "$workers" | grep -c . || true)"
  if [ "$count" -gt 1 ] || { [ "$count" -eq 1 ] && [ "$FORCE_KILL_SINGLE" = 1 ]; }; then
    local victim
    victim="$(printf '%s\n' "$workers" | head -n1)"
    log "+${KILL_AT}s docker kill ${victim} (of ${count} workers)"
    docker kill "$victim" >/dev/null
    echo "$victim" >"${RAW_DIR}/t4.killed"
  else
    log "+${KILL_AT}s only ${count} worker running: kill skipped (scale with --scale ${WORKER_SERVICE}=2 or FORCE_KILL_SINGLE=1)"
  fi
}

rm -f "${RAW_DIR}/t4.killed"
log "load: ${RATE} ev/s for ${DURATION}, run id ${RUN_ID}"
chaos &
CHAOS_PID=$!

K6_STATUS=0
k6 run --quiet --summary-export "${RAW_DIR}/t4.json" loadtest/k6/t1_latency.js || K6_STATUS=$?
wait "$CHAOS_PID" || true
log "k6 finished (exit ${K6_STATUS}; non-zero only means a k6 threshold was crossed)"

if [ -f "${RAW_DIR}/t4.killed" ]; then
  KILLED=1
  victim="$(cat "${RAW_DIR}/t4.killed")"
  log "restarting killed worker ${victim}"
  docker start "$victim" >/dev/null || log "could not restart ${victim}"
fi

log "mockgw state:"
curl -fsS "${MOCKGW_URL}/admin/state" || true
echo

log "waiting for drain and reconciling"
RECON_STATUS=0
if [ "$KILLED" = 1 ]; then
  uv run loadtest/reconcile.py chaos --run-file "${RAW_DIR}/t4.run.json" --killed | tee "${RAW_DIR}/t4.reconcile.txt" || RECON_STATUS=$?
else
  uv run loadtest/reconcile.py chaos --run-file "${RAW_DIR}/t4.run.json" | tee "${RAW_DIR}/t4.reconcile.txt" || RECON_STATUS=$?
fi

exit "$RECON_STATUS"
