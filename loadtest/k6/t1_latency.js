// T1 latency: constant arrival rate of mixed events (spec §19).
//
//   k6 run --summary-export loadtest/results/raw/t1.json loadtest/k6/t1_latency.js
//
// Env: API_URL (http://localhost:8080)  RATE (100 ev/s)  DURATION (5m)
//      MATCH_RATIO (0.2)  TYPED_RATIO (0.02)  RUN_ID  NAME (t1)  RAW_DIR
//      PRE_VUS (50)  MAX_VUS (400)  HTTP_P99_MS (500)
import http from 'k6/http';
import { sleep } from 'k6';
import {
  newRunId, pickEvent, sentByKind, eventsAccepted, eventsRejected, expectAlert, expectAlertTyped,
} from './lib/events.js';
import { buildSummary, TREND_STATS } from './lib/summary.js';

const NAME = __ENV.NAME || 't1';
const RATE = parseInt(__ENV.RATE || '100', 10);
const DURATION = __ENV.DURATION || '5m';
const MATCH_RATIO = parseFloat(__ENV.MATCH_RATIO || '0.2');
const TYPED_RATIO = parseFloat(__ENV.TYPED_RATIO || '0.02');

export const options = {
  scenarios: {
    mixed: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: parseInt(__ENV.PRE_VUS || '50', 10),
      maxVUs: parseInt(__ENV.MAX_VUS || '400', 10),
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: [`p(99)<${__ENV.HTTP_P99_MS || '500'}`],
  },
  summaryTrendStats: TREND_STATS,
};

export function setup() {
  return { runId: newRunId(), startedAt: new Date().toISOString() };
}

export default function (setupData) {
  const uid = `${setupData.runId}-${__VU}-${__ITER}`;
  const ev = pickEvent({
    matchRatio: MATCH_RATIO, typedRatio: TYPED_RATIO, uid, n: __ITER, runId: setupData.runId,
  });
  const res = http.post(ev.url, ev.body, { headers: ev.headers, tags: { name: ev.tag }, timeout: '10s' });
  sentByKind[ev.kind].add(1);
  if (res.status === 202) {
    eventsAccepted.add(1);
    if (ev.createsAlert) (ev.kind === 'typed' ? expectAlertTyped : expectAlert).add(1);
  } else {
    eventsRejected.add(1);
    if (res.status === 503 || res.status === 429) {
      const wait = parseFloat((res.headers['Retry-After'] || '0'));
      if (wait > 0) sleep(Math.min(wait, 2));
    }
  }
}

export function handleSummary(data) {
  return buildSummary(NAME, data, { rate: RATE, matchRatio: MATCH_RATIO, typedRatio: TYPED_RATIO });
}
