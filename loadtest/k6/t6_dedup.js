// T6 dedup: TOTAL identical WORKFLOW_FAILED prod events spread over DURATION_S seconds.
// Expected: exactly 1 alert with count == TOTAL and exactly one set of hard deliveries
// (verify with: uv run loadtest/reconcile.py dedup --run-id <runId> --total <TOTAL>).
//
//   k6 run --summary-export loadtest/results/raw/t6.json loadtest/k6/t6_dedup.js
//
// Env: API_URL  TOTAL (500)  DURATION_S (60)  RUN_ID  NAME (t6)  RAW_DIR  KEY_WORKFLOW
import http from 'k6/http';
import { sleep } from 'k6';
import { newRunId, identicalWorkflowFailed, KEYS, API_URL, eventsAccepted, eventsRejected } from './lib/events.js';
import { buildSummary, TREND_STATS } from './lib/summary.js';

const NAME = __ENV.NAME || 't6';
const TOTAL = parseInt(__ENV.TOTAL || '500', 10);
const DURATION_S = parseInt(__ENV.DURATION_S || '60', 10);
const VUS = Math.min(20, TOTAL);
// Each VU sends one event, then waits so that all VUs together spread TOTAL events over DURATION_S.
const PACE_S = (DURATION_S * VUS) / TOTAL;

export const options = {
  scenarios: {
    identical: { executor: 'shared-iterations', vus: VUS, iterations: TOTAL, maxDuration: `${DURATION_S * 3}s` },
  },
  thresholds: { http_req_failed: ['rate<0.01'] },
  summaryTrendStats: TREND_STATS,
};

export function setup() {
  return { runId: newRunId(), startedAt: new Date().toISOString() };
}

export default function (setupData) {
  const body = JSON.stringify(identicalWorkflowFailed(setupData.runId));
  const res = http.post(`${API_URL}/v1/ingest/${KEYS.workflow}`, body, {
    headers: { 'Content-Type': 'application/json' },
    tags: { name: 'ingest_workflow' },
    timeout: '10s',
  });
  if (res.status === 202) eventsAccepted.add(1);
  else eventsRejected.add(1);
  sleep(PACE_S);
}

export function handleSummary(data) {
  return buildSummary(NAME, data, { total: TOTAL, windowS: DURATION_S });
}
