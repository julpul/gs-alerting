// Event generators for the five seeded sources (spec Appendix A + §7.1 rules).
//
// Every generated event carries the run id and a unique id inside the fields
// that feed dedup keys / titles, so a "matching" event creates a NEW alert
// instead of collapsing into an existing one (except in t6_dedup.js, which
// deliberately sends identical events).
import { Counter } from 'k6/metrics';

export const KEYS = {
  workflow: __ENV.KEY_WORKFLOW || 'demo-key-workflow-broker',
  prometheus: __ENV.KEY_PROMETHEUS || 'demo-key-prometheus',
  pricing: __ENV.KEY_PRICING || 'demo-key-pricing-logs',
  trade: __ENV.KEY_TRADE || 'demo-key-trade-gateway',
  typed: __ENV.KEY_TYPED || 'demo-key-pr-review-agent',
};

export const API_URL = (__ENV.API_URL || 'http://localhost:8080').replace(/\/$/, '');

// Counters shared by all scripts; the reconciliation compares them with the DB.
export const sentByKind = {
  workflow: new Counter('events_sent_workflow'),
  prometheus: new Counter('events_sent_prometheus'),
  pricing: new Counter('events_sent_pricing'),
  trade: new Counter('events_sent_trade'),
  typed: new Counter('events_sent_typed'),
};
export const eventsAccepted = new Counter('events_accepted');
export const eventsRejected = new Counter('events_rejected');
// Accepted events that must each produce exactly one NEW alert (unique dedup key, matching rule).
export const expectAlert = new Counter('events_expect_alert');
export const expectAlertTyped = new Counter('events_expect_alert_typed');

const PRICING_INSTRUMENTS = ['EURUSD', 'GBPUSD', 'USDJPY', 'EURPLN', 'USDPLN', 'XAUUSD', 'BUND10', 'UST10'];
const PRICING_LEVELS_MATCH = ['WARN', 'ERROR', 'FATAL'];
const TRADE_CCY = ['EUR', 'USD', 'GBP', 'PLN'];
const TRADE_REASONS = ['LIMIT_BREACH', 'CREDIT_CHECK', 'STALE_PRICE'];

const uniqueTitles = (__ENV.UNIQUE_TITLES || '1') !== '0';

function pick(arr, n) {
  return arr[n % arr.length];
}

function isoNow() {
  return new Date().toISOString();
}

export function newRunId() {
  return __ENV.RUN_ID || `lt${Date.now().toString(36)}${Math.floor(Math.random() * 1296).toString(36)}`;
}

// Appendix A sample 1: WORKFLOW_FAILED. Matches Team Settlements only for prod + order-settlement.
export function workflowFailed({ match, uid, runId }) {
  const step = uniqueTitles ? `validate-counterparty-${uid}` : 'validate-counterparty';
  return {
    type: 'WORKFLOW_FAILED',
    workflowId: `WF-${uid}`,
    workflowName: 'order-settlement',
    step,
    severity: 'ERROR',
    environment: match ? 'prod' : 'staging',
    timestamp: isoNow(),
    error: { code: 'TIMEOUT', message: `Counterparty service did not respond in 5000 ms (${uid})` },
    loadRunId: runId,
  };
}

// Appendix A sample 2: Alertmanager-like firing alert. Matches Anna's rule for critical + payments-api.
export function alertmanager({ match, uid, runId }) {
  return {
    alertname: `HighErrorRate-${uid}`,
    labels: {
      service: match ? 'payments-api' : 'catalog-api',
      region: 'eu-west',
      severity: match ? 'critical' : 'warning',
      instance: `payments-${uid}`,
    },
    annotations: { summary: `Error rate 12% over the last 5 minutes (${uid})` },
    startsAt: isoNow(),
    status: 'firing',
    loadRunId: runId,
  };
}

// Pricing-engine log line. Matches Team Pricing for WARN/ERROR/FATAL; grouped per instrument per 5 min.
export function pricingLog({ match, uid, n, runId }) {
  const level = match ? pick(PRICING_LEVELS_MATCH, n) : 'INFO';
  return {
    level,
    logger: `pricing-engine-${(n % 4) + 1}`,
    instrument: pick(PRICING_INSTRUMENTS, n),
    latencyMs: match ? 800 + (n % 700) : 12 + (n % 30),
    message: match ? `quote latency above threshold (${uid})` : `quote published (${uid})`,
    loadRunId: runId,
  };
}

// Appendix A sample 3: trade.rejected.v2 envelope. Matches Team Rates for desk=rates and notional > 10M.
export function tradeRejected({ match, uid, n, runId }) {
  return {
    meta: { schema: 'trade.rejected.v2', producer: 'trade-gateway', ts: Date.now() },
    payload: {
      tradeId: `T-${uid}`,
      desk: match ? 'rates' : 'fx',
      notional: match ? 10000001 + n : 50000 + (n % 1000),
      currency: pick(TRADE_CCY, n),
      reason: pick(TRADE_REASONS, n),
    },
    loadRunId: runId,
  };
}

// Typed raise (POST /v1/alerts/raise, X-Api-Key). dedupKey is unique => always a new alert.
export function typedRaise({ uid, runId }) {
  return {
    type: 'security.secret_leaked',
    severity: 'critical',
    title: `Secret leaked in PR ${uid}`,
    description: 'Automated review found a credential-like string in a diff.',
    dedupKey: `pr-${uid}`,
    attributes: { loadRunId: runId },
  };
}

const BUILDERS = {
  workflow: workflowFailed,
  prometheus: alertmanager,
  pricing: pricingLog,
  trade: tradeRejected,
};

// Weights used when choosing the source of one event.
const MIX_ALL = { workflow: 30, prometheus: 25, pricing: 25, trade: 20 };
// Matching events: pricing only groups (fewer new alerts), so it is down-weighted.
const MIX_MATCHING = { workflow: 35, prometheus: 30, pricing: 10, trade: 25 };

function weighted(mix, r) {
  const total = Object.values(mix).reduce((a, b) => a + b, 0);
  let x = r * total;
  for (const [k, w] of Object.entries(mix)) {
    x -= w;
    if (x < 0) return k;
  }
  return Object.keys(mix)[0];
}

// Kinds whose matching events always create one new alert each.
const ALERT_PER_EVENT = new Set(['workflow', 'prometheus', 'trade']);

// One event for the single-event endpoint (or typed raise).
//   matchRatio: share of events that match a seeded rule (0..1)
//   typedRatio: share of events sent as typed raises
export function pickEvent({ matchRatio, typedRatio, uid, n, runId }) {
  if (typedRatio > 0 && Math.random() < typedRatio) {
    return {
      kind: 'typed',
      matching: true,
      createsAlert: true,
      url: `${API_URL}/v1/alerts/raise`,
      headers: { 'Content-Type': 'application/json', 'X-Api-Key': KEYS.typed },
      body: JSON.stringify(typedRaise({ uid, runId })),
      tag: 'raise',
    };
  }
  const match = Math.random() < matchRatio;
  const kind = weighted(match ? MIX_MATCHING : MIX_ALL, Math.random());
  return {
    kind,
    matching: match,
    createsAlert: match && ALERT_PER_EVENT.has(kind),
    url: `${API_URL}/v1/ingest/${KEYS[kind]}`,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(BUILDERS[kind]({ match, uid, n, runId })),
    tag: `ingest_${kind}`,
  };
}

// A batch of `size` events for one source (batch endpoint is per source key).
// The OpenAPI contract says the body is a JSON array; BATCH_WRAP=1 sends {"events":[...]} instead.
export function pickBatch({ matchRatio, size, uid, runId }) {
  const kind = weighted(MIX_ALL, Math.random());
  const events = [];
  let alertEvents = 0;
  for (let i = 0; i < size; i++) {
    const match = Math.random() < matchRatio;
    events.push(BUILDERS[kind]({ match, uid: `${uid}-${i}`, n: i, runId }));
    if (match && ALERT_PER_EVENT.has(kind)) alertEvents++;
  }
  const body = (__ENV.BATCH_WRAP || '0') === '1' ? { events } : events;
  return {
    kind,
    size,
    alertEvents,
    url: `${API_URL}/v1/ingest/${KEYS[kind]}/batch`,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    tag: `batch_${kind}`,
  };
}

// The 500 identical events of T6: same workflowId, step, message and timestamp.
export function identicalWorkflowFailed(runId) {
  return {
    type: 'WORKFLOW_FAILED',
    workflowId: `WF-DEDUP-${runId}`,
    workflowName: 'order-settlement',
    step: `validate-counterparty-${runId}`,
    severity: 'ERROR',
    environment: 'prod',
    timestamp: '2026-10-23T09:14:02.331Z',
    error: { code: 'TIMEOUT', message: 'Counterparty service did not respond in 5000 ms' },
    loadRunId: runId,
  };
}
