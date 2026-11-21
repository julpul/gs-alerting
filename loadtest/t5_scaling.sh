#!/usr/bin/env bash
# T5 scaling: run a (shortened) T2 with 1, 2 and 4 worker replicas and record the
# maximum sustained ev/s per replica count in loadtest/results/raw/t5.json.
#
# Env (defaults):
#   WORKERS_LIST "1 2 4"   COMPOSE_FILE infra/docker-compose.yml   COMPOSE_PROJECT gsalert
#   WORKER_SERVICE worker  STEP_RATE 200  STEP_SECONDS 20  MAX_RATE 4000  BATCH_SIZE 100
#   SETTLE_SECONDS 15 (wait after scaling)   DRAIN_TIMEOUT 300   RAW_DIR loadtest/results/raw
#   plus everything t2_throughput.js reads (API_URL, PROM_URL, RABBIT_URL, ...)
set -euo pipefail

cd "$(dirname "$0")/.."

WORKERS_LIST="${WORKERS_LIST:-1 2 4}"
COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.yml}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-gsalert}"
WORKER_SERVICE="${WORKER_SERVICE:-worker}"
SETTLE_SECONDS="${SETTLE_SECONDS:-15}"
export RAW_DIR="${RAW_DIR:-loadtest/results/raw}"
export STEP_RATE="${STEP_RATE:-200}"
export STEP_SECONDS="${STEP_SECONDS:-20}"
export MAX_RATE="${MAX_RATE:-4000}"
export BATCH_SIZE="${BATCH_SIZE:-100}"

mkdir -p "$RAW_DIR"
log() { printf '[t5 %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

running_workers() {
  docker ps -q \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" \
    --filter "label=com.docker.compose.service=${WORKER_SERVICE}" | grep -c . || true
}

scale_workers() {
  docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" up -d --no-recreate --scale "${WORKER_SERVICE}=$1" "$WORKER_SERVICE"
}

original="$(running_workers)"
[ "$original" -gt 0 ] || original=1

for n in $WORKERS_LIST; do
  log "scaling ${WORKER_SERVICE} to ${n}"
  scale_workers "$n"
  for _ in $(seq 1 60); do
    [ "$(running_workers)" -eq "$n" ] && break
    sleep 1
  done
  sleep "$SETTLE_SECONDS"
  uv run loadtest/reconcile.py drain || log "queues not empty before the run; results for ${n} workers may be pessimistic"
  log "T2 with ${n} worker(s)"
  NAME="t2_w${n}" k6 run --quiet --summary-export "${RAW_DIR}/t2_w${n}.json" loadtest/k6/t2_throughput.js \
    || log "k6 exited non-zero (a threshold abort is the expected way for T2 to end)"
done

log "restoring ${WORKER_SERVICE} to ${original}"
scale_workers "$original"

WORKERS_LIST="$WORKERS_LIST" RAW_DIR="$RAW_DIR" python3 - <<'PY'
import json
import os
import pathlib

raw = pathlib.Path(os.environ["RAW_DIR"])
results = []
for n in os.environ["WORKERS_LIST"].split():
    try:
        run = json.loads((raw / f"t2_w{n}.run.json").read_text())
    except (OSError, ValueError):
        continue
    results.append({
        "workers": int(n),
        "maxSustainedEvPerSec": run.get("maxSustainedEvPerSec"),
        "acceptedEvPerSec": run.get("acceptedEvPerSec"),
        "stopReason": run.get("stopReason"),
        "startedAt": run.get("startedAt"),
        "endedAt": run.get("endedAt"),
    })
(raw / "t5.json").write_text(json.dumps({"results": results}, indent=2))
print("T5 workers -> max sustained ev/s")
for r in results:
    print(f"  {r['workers']:>2} -> {r['maxSustainedEvPerSec']}")
PY
