# Load tests (T1 – T6)

k6 scripts, a chaos script, a reconciliation tool and a report generator that back the performance
claims in `docs/TECH_SPEC.md` §18/§19.

| Test | Question | Run | Output |
| --- | --- | --- | --- |
| T1 latency | How fast does an alert reach a human? | `make t1` | k6 summary + SQL p50/p99 per channel |
| T2 throughput | How much can we take? | `make t2` | max sustained ev/s |
| T3 queue | Does the queue clog? | Grafana during T2 | queue depth rises and drains |
| T4 no loss | Does anything get lost? | `make t4` | `LOST = 0` |
| T5 scaling | Do replicas help? | `make t5` | ev/s at 1, 2, 4 workers |
| T6 dedup | One alert for 500 errors? | `make t6` | 1 alert, count 500, 1 hard delivery each |

## Prerequisites

```bash
brew install k6          # load generator (tested with k6 2.x)
brew install uv          # runs report.py / reconcile.py with inline dependencies
brew install shellcheck  # optional, lint for the shell scripts
make up                  # full stack: api, workers, rabbitmq, postgres, mockgw, seeded sources/rules
```

Optional but strongly recommended: Prometheus + Grafana (processing latency, outcome counters, queue depth).

```bash
docker compose -p gsalert-monitoring -f loadtest/compose.monitoring.yml up -d
# Prometheus http://localhost:9090   Grafana http://localhost:3001 -> dashboard "Alerting – load"
```

The `make` targets live in `loadtest/loadtest.mk` (the root Makefile includes it). Without make:
`k6 run --summary-export loadtest/results/raw/t1.json loadtest/k6/t1_latency.js`.

## Running

Common env: `API_URL` (`http://localhost:8080`), `DATABASE_URL`
(`postgresql://alerting:alerting@localhost:5432/alerting`), `PROM_URL` (`http://localhost:9090`),
`RABBIT_URL` (`http://localhost:15672`, guest/guest), `MOCKGW_URL` (`http://localhost:8090`).

| Command | What it does | Main knobs |
| --- | --- | --- |
| `make t1` | Constant arrival rate, mixed Appendix A events (workflow, Alertmanager, pricing log, trade, typed raise), 20 % matching a seeded rule, unique dedup keys so matching events create new alerts | `RATE=100` `DURATION=5m` `MATCH_RATIO=0.2` `TYPED_RATIO=0.02` |
| `make t2` | `ramping-arrival-rate` on `POST /v1/ingest/{key}/batch`, +`STEP_RATE` ev/s every `STEP_SECONDS` up to `MAX_RATE`. Aborts on http errors > 1 %, http p99 > 1 s, `events.in` backlog > 1 s of traffic or processing p99 > 1 s (Prometheus) | `STEP_RATE=200` `STEP_SECONDS=30` `MAX_RATE=4000` `BATCH_SIZE=100` |
| `make t4` | 200 ev/s for 3 min; at +30 s SMS gateway fails for 60 s; at +60 s `docker kill` one worker (restarted later, skipped if only one runs); waits for queues to drain; reconciles | `RATE=200` `LOAD_SECONDS=180` `FAIL_AT=30` `KILL_AT=60` `SMS_FAIL_SECONDS=60` `FORCE_KILL_SINGLE=1` `DRAIN_TIMEOUT=300` |
| `make t5` | T2 (20 s steps) at 1, 2 and 4 workers via `docker compose --scale worker=N`; restores the original scale | `WORKERS_LIST="1 2 4"` `STEP_SECONDS=20` `COMPOSE_FILE` `COMPOSE_PROJECT=gsalert` |
| `make t6` | 500 identical `WORKFLOW_FAILED` prod events over 60 s, then checks 1 alert with count 500 and one hard delivery per channel/recipient | `TOTAL=500` `DURATION_S=60` |
| `make report` | Writes `loadtest/results/<YYYY-MM-DD_HHMM>/report.md` + PNGs | `REPORT_ARGS="--since 2h --until ..."` |
| `make loadtest-smoke` | 30 s T1 at 20 ev/s + T6 with 50 events, dedup verified (CI) | – |
| `make loadtest-test` | Unit tests of the report / reconciliation code, no infrastructure | – |
| `make loadtest-clean` | Deletes `loadtest/results/raw/*` | – |

T4 needs at least two workers to kill one: `docker compose -p gsalert -f infra/docker-compose.yml up -d --scale worker=2`.
Do not run T4 with `DEMO_FAST_ESCALATION` on: escalation re-notifications of the generated alerts would add deliveries
that are still in flight when the reconciliation reads the table.

Example sequence for the video / slides:

```bash
make t1 && make t6 && make t4 && make t2 && make t5 && make report
```

## Where results go

- `loadtest/results/raw/` (git-ignored): `<test>.json` is the k6 `--summary-export`; `<test>.run.json` is written by the
  script itself (run id, wall-clock window, accepted/rejected counts, T2 `maxSustainedEvPerSec`); `t4.reconcile.txt`,
  `t5.json`.
- `loadtest/results/<date>/report.md` and `*.png`: the report (commit these for the README / slides).

### How to read the report

- **Delivery latency per channel** (SQL): `deliveries.delivered_at - deliveries.event_received_at`, i.e. ingest to
  provider/UI. Alert creation latency: `alerts.created_at - alerts.first_event_received_at`.
- **Processing latency** (Prometheus): `alerting_processing_latency_seconds`, worker commit minus `received_at`, every event.
- **k6 http latency** is the time the API needs to accept the event (publisher confirm included), not the e2e latency.
- A row marked `n/a` means that source was unavailable (no k6 run, DB or Prometheus unreachable).

### Targets (spec §18)

| Metric | Target |
| --- | --- |
| e2e ingest to UI at 100 ev/s | p50 < 50 ms, p99 < 300 ms |
| e2e ingest to mock SMS/email sent at 100 ev/s | p99 < 1 s |
| Sustained throughput, laptop, 4 workers | >= 1,000 ev/s with processing p99 < 1 s |
| Lost alerts in chaos test | 0 |
| Dedup | 500 identical -> 1 alert, 1 hard notification |

The report marks the per-channel rows PASS/FAIL against these.

## How T4 decides `LOST`

`reconcile.py chaos` (called by `t4_chaos.sh`) first waits until `events.in`, `deliveries.{ui,email,sms,phone,webhook}`
and `deliveries.retry.*` are empty (the DLQ is excluded), then adds up three independent checks:

1. **Deliveries**: for the alerts of this run (found through the run id embedded in `raw_event`),
   `created = delivered(sent|delivered|acked) + dead_lettered with a delivered fallback on another channel + suppressed_digest`.
   Anything else (still pending/retrying, dead-lettered without fallback) is lost.
2. **Alerts**: every accepted, matching, unique-key event of the run must have produced an alert
   (k6 counts them in `events_expect_alert`; pricing alerts are grouped and excluded).
3. **Counters** (needs Prometheus): accepted events (k6 202s) minus the sum of `alerting_events_outcome_total` increases.
   Because a `docker kill`ed worker loses its last unscraped counter increments (< 2 s of its traffic), a gap up to
   `COUNTER_SLACK` (default 500) is tolerated, and only when a worker was actually killed. Checks 1 and 2 read the
   database and are the ground truth.

The script prints `LOST = N` and exits 1 when `N > 0`. The same exit code makes `make t4` usable in CI.
Also printed: `GET /v1/metrics/summary` (`lost`, `eventsIngested`) for comparison with what the UI shows.

## Contract notes

- Batch body: `docs/openapi.yaml` defines a **JSON array** of events. `BATCH_WRAP=1` sends `{"events": [...]}` instead.
- Every generated event carries `loadRunId` (and unique `workflowId` / `tradeId` / alertname / dedupKey) so a run can be
  found again in `alerts.raw_event`. `UNIQUE_TITLES=0` keeps the step name constant (dedup by title may then collapse alerts).
- Typed raises (`/v1/alerts/raise`, key `demo-key-pr-review-agent`) are part of T1 (`TYPED_RATIO`), excluded from T4.
- The seeded source keys can be overridden with `KEY_WORKFLOW`, `KEY_PROMETHEUS`, `KEY_PRICING`, `KEY_TRADE`, `KEY_TYPED`.

## Wiring the stack must provide

1. Root Makefile: `include loadtest/loadtest.mk`.
2. **Metrics endpoints**: API `GET :8080/metrics`; `worker`, `delivery`, `scheduler`, `ai` containers each expose Prometheus
   metrics on **:9101** (`prometheus_client.start_http_server(9101)` in the role runner). Histogram
   `alerting_processing_latency_seconds` must exist in worker processes and `alerting_delivery_latency_seconds{channel}`
   in delivery processes; names in plan §3.6.
3. **RabbitMQ Prometheus plugin** (port **15692**): `rabbitmq_prometheus` is enabled by default in the
   `rabbitmq:3.13-management` image; otherwise `rabbitmq-plugins enable rabbitmq_prometheus`. `infra/prometheus.yml`
   reads per-queue depth from `/metrics/detailed?family=queue_coarse_metrics`
   (`rabbitmq_detailed_queue_messages_ready{queue=...}`), so no `return_per_object_metrics` setting is needed.
4. **Prometheus + Grafana**: either the services in `infra/docker-compose.yml` mounting `infra/prometheus.yml`,
   `infra/grafana/provisioning` -> `/etc/grafana/provisioning` and `infra/grafana/dashboards` -> `/var/lib/grafana/dashboards`,
   or `loadtest/compose.monitoring.yml` (joins the external network `gsalert_default`; do not run both, ports clash).
5. Compose project name `gsalert` (containers `gsalert-worker-1`, network `gsalert_default`); services named
   `worker`, `delivery`, `scheduler`, `ai`, `api`, `rabbitmq` (Prometheus uses these DNS names; `worker` and `delivery`
   via DNS service discovery so every replica is scraped).
6. Seeded sources/rules per spec §7.1/§21 with the keys listed above, `master@demo` / `demo`.

## Developing without the backend

```bash
uv run --with pytest --with matplotlib --with asyncpg --with httpx pytest loadtest/tests -q   # same as `make loadtest-test`
k6 inspect loadtest/k6/t1_latency.js                                                         # syntax check of each script
python3 loadtest/tests/fake_api.py 18080 &   # always answers 202; mkdir -p /tmp/raw first
API_URL=http://localhost:18080 RABBIT_URL=http://localhost:18080 PROM_URL= DURATION=10s RATE=20 \
  RAW_DIR=/tmp/raw k6 run --summary-export /tmp/raw/t1.json loadtest/k6/t1_latency.js
```

The tests feed fake k6 summaries and fake SQL results into `report.py` (markdown and PNG output) and exercise the
reconciliation arithmetic of `reconcile.py` (including counter resets of a killed worker and queue draining).
