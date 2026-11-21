// T2 throughput: ramping arrival rate through the batch endpoint (spec §19).
// The rate goes up by STEP_RATE ev/s every STEP_SECONDS until MAX_RATE; the test
// aborts when ingest degrades (http errors > 1 %, http p99 > 1 s) or the workers
// fall behind (events.in backlog > BACKLOG_LIMIT_S seconds of current traffic,
// or processing p99 from Prometheus > 1 s).
//
//   k6 run --summary-export loadtest/results/raw/t2.json loadtest/k6/t2_throughput.js
//
// Env: API_URL  STEP_RATE (200)  STEP_SECONDS (30)  MAX_RATE (4000)  START_RATE (=STEP_RATE)
//      BATCH_SIZE (100)  MATCH_RATIO (0.2)  NAME (t2)  RUN_ID  RAW_DIR
//      HTTP_P99_MS (1000)  PROCESSING_P99_S (1)  BACKLOG_LIMIT_S (1)  WATCH_SECONDS (3)
//      PROM_URL (http://localhost:9090, "" disables)  RABBIT_URL (http://localhost:15672)  RABBIT_AUTH (guest:guest)
//      BATCH_WRAP=1 sends {"events":[...]} instead of a bare array
import http from 'k6/http';
import encoding from 'k6/encoding';
import { sleep } from 'k6';
import { Gauge, Counter } from 'k6/metrics';
import { newRunId, pickBatch, sentByKind, eventsAccepted, eventsRejected, expectAlert } from './lib/events.js';
import { buildSummary, counterValue, TREND_STATS } from './lib/summary.js';

const NAME = __ENV.NAME || 't2';
const STEP_RATE = parseInt(__ENV.STEP_RATE || '200', 10);
const START_RATE = parseInt(__ENV.START_RATE || String(STEP_RATE), 10);
const STEP_SECONDS = parseInt(__ENV.STEP_SECONDS || '30', 10);
const MAX_RATE = parseInt(__ENV.MAX_RATE || '4000', 10);
const BATCH_SIZE = parseInt(__ENV.BATCH_SIZE || '100', 10);
const MATCH_RATIO = parseFloat(__ENV.MATCH_RATIO || '0.2');
const WATCH_SECONDS = parseFloat(__ENV.WATCH_SECONDS || '3');
const PROM_URL = (__ENV.PROM_URL === undefined ? 'http://localhost:9090' : __ENV.PROM_URL).replace(/\/$/, '');
const RABBIT_URL = (__ENV.RABBIT_URL || 'http://localhost:15672').replace(/\/$/, '');
const RABBIT_AUTH = encoding.b64encode(__ENV.RABBIT_AUTH || 'guest:guest');

const STEPS = Math.max(1, Math.floor((MAX_RATE - START_RATE) / STEP_RATE) + 1);
const TOTAL_S = STEPS * STEP_SECONDS;

// ev/s -> batch requests per second (arrival rate executor wants iterations per timeUnit).
const iterPerSec = (evPerSec) => Math.max(1, Math.round(evPerSec / BATCH_SIZE));

const stages = [];
for (let i = 0; i < STEPS; i++) {
  stages.push({ target: iterPerSec(START_RATE + i * STEP_RATE), duration: '1s' });
  stages.push({ target: iterPerSec(START_RATE + i * STEP_RATE), duration: `${STEP_SECONDS - 1}s` });
}

const processingP99 = new Gauge('processing_p99_s');
const backlogSeconds = new Gauge('events_in_backlog_s');
const promFailures = new Counter('watch_prom_failures');

const abort = (threshold, delay = '10s') => [{ threshold, abortOnFail: true, delayAbortEval: delay }];

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-arrival-rate',
      startRate: iterPerSec(START_RATE),
      timeUnit: '1s',
      stages,
      preAllocatedVUs: parseInt(__ENV.PRE_VUS || '50', 10),
      maxVUs: parseInt(__ENV.MAX_VUS || '1000', 10),
      exec: 'ingest',
    },
    watch: { executor: 'constant-vus', vus: 1, duration: `${TOTAL_S}s`, exec: 'watch' },
  },
  thresholds: {
    http_req_failed: abort('rate<0.01'),
    http_req_duration: abort(`p(99)<${__ENV.HTTP_P99_MS || '1000'}`),
    processing_p99_s: abort(`value<${__ENV.PROCESSING_P99_S || '1'}`, __ENV.ABORT_GRACE || '20s'),
    events_in_backlog_s: abort(`value<${__ENV.BACKLOG_LIMIT_S || '1'}`, __ENV.ABORT_GRACE || '20s'),
  },
  summaryTrendStats: TREND_STATS,
};

export function setup() {
  return { runId: newRunId(), startedAt: new Date().toISOString(), t0: Date.now() };
}

export function ingest(setupData) {
  const b = pickBatch({
    matchRatio: MATCH_RATIO, size: BATCH_SIZE, uid: `${setupData.runId}-${__VU}-${__ITER}`, runId: setupData.runId,
  });
  const res = http.post(b.url, b.body, { headers: b.headers, tags: { name: b.tag }, timeout: '15s' });
  sentByKind[b.kind].add(b.size);
  if (res.status === 202) {
    eventsAccepted.add(b.size);
    expectAlert.add(b.alertEvents);
  } else {
    eventsRejected.add(b.size);
  }
}

function currentTargetRate(setupData) {
  const elapsed = (Date.now() - setupData.t0) / 1000;
  const step = Math.min(STEPS - 1, Math.max(0, Math.floor(elapsed / STEP_SECONDS)));
  return START_RATE + step * STEP_RATE;
}

function promScalar(query) {
  const res = http.get(`${PROM_URL}/api/v1/query?query=${encodeURIComponent(query)}`, { timeout: '2s', tags: { name: 'watch_prom' } });
  if (res.status !== 200) return null;
  const result = res.json('data.result');
  if (!result || result.length === 0) return null;
  const v = parseFloat(result[0].value[1]);
  return Number.isFinite(v) ? v : null;
}

// Runs once per WATCH_SECONDS: reads the queue backlog from RabbitMQ and, if reachable,
// the processing p99 from Prometheus. Gauges feed the abort thresholds above.
export function watch(setupData) {
  const q = http.get(`${RABBIT_URL}/api/queues/%2F/events.in`, {
    headers: { Authorization: `Basic ${RABBIT_AUTH}` }, timeout: '2s', tags: { name: 'watch_rabbit' },
  });
  if (q.status === 200) {
    const ready = q.json('messages_ready') || 0;
    backlogSeconds.add(ready / Math.max(1, currentTargetRate(setupData)));
  }
  if (PROM_URL) {
    const p99 = promScalar(
      'histogram_quantile(0.99, sum by (le) (rate(alerting_processing_latency_seconds_bucket[10s])))',
    );
    if (p99 === null) promFailures.add(1);
    else processingP99.add(p99);
  }
  sleep(WATCH_SECONDS);
}

export function handleSummary(data) {
  const elapsedS = (data.state && data.state.testRunDurationMs ? data.state.testRunDurationMs : 0) / 1000;
  const aborted = elapsedS < TOTAL_S - 2;
  // Steps that ran to completion; the step during which the abort happened is not "sustained".
  const completed = aborted ? Math.floor(elapsedS / STEP_SECONDS) : STEPS;
  const maxSustained = completed > 0 ? START_RATE + (completed - 1) * STEP_RATE : 0;
  const reason = aborted ? 'threshold abort (http errors / http p99 / processing p99 / backlog)' : 'completed all steps';
  return buildSummary(NAME, data, {
    stepRate: STEP_RATE,
    stepSeconds: STEP_SECONDS,
    maxRate: MAX_RATE,
    batchSize: BATCH_SIZE,
    stepsCompleted: completed,
    aborted,
    stopReason: reason,
    maxSustainedEvPerSec: maxSustained,
    acceptedEvPerSec: elapsedS > 0 ? Math.round(counterValue(data, 'events_accepted') / elapsedS) : 0,
  });
}
