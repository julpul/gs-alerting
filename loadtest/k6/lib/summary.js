// Shared handleSummary helper: prints a short text summary and writes
// <RAW_DIR>/<name>.run.json with what the report / reconciliation need
// (run id, wall-clock window, counters). The k6 --summary-export file is
// written by the CLI flag next to it as <RAW_DIR>/<name>.json.

export const RAW_DIR = (__ENV.RAW_DIR || 'loadtest/results/raw').replace(/\/$/, '');

export const TREND_STATS = ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'];

function metricValues(data, name) {
  const m = data.metrics[name];
  if (!m) return null;
  return m.values || m;
}

export function counterValue(data, name) {
  const v = metricValues(data, name);
  return v && typeof v.count === 'number' ? v.count : 0;
}

export function trendValue(data, name, stat) {
  const v = metricValues(data, name);
  return v && typeof v[stat] === 'number' ? v[stat] : null;
}

function fmt(n, digits = 1) {
  return n === null || n === undefined ? 'n/a' : Number(n).toFixed(digits);
}

export function buildSummary(name, data, extra = {}) {
  const endedAt = new Date();
  const durationMs = data.state && data.state.testRunDurationMs ? data.state.testRunDurationMs : 0;
  const setup = data.setup_data || {};
  const run = {
    test: name,
    runId: setup.runId || __ENV.RUN_ID || null,
    startedAt: setup.startedAt || new Date(endedAt.getTime() - durationMs).toISOString(),
    endedAt: endedAt.toISOString(),
    durationS: Math.round(durationMs / 100) / 10,
    apiUrl: __ENV.API_URL || 'http://localhost:8080',
    counts: {
      sent: counterValue(data, 'http_reqs'),
      accepted: counterValue(data, 'events_accepted'),
      rejected: counterValue(data, 'events_rejected'),
      expectedAlerts: counterValue(data, 'events_expect_alert'),
      expectedAlertsTyped: counterValue(data, 'events_expect_alert_typed'),
      droppedIterations: counterValue(data, 'dropped_iterations'),
    },
    httpMs: {
      p50: trendValue(data, 'http_req_duration', 'med'),
      p95: trendValue(data, 'http_req_duration', 'p(95)'),
      p99: trendValue(data, 'http_req_duration', 'p(99)'),
    },
    ...extra,
  };
  const text = [
    '',
    `== ${name} ==  run ${run.runId}  ${run.durationS}s`,
    `  events accepted ${run.counts.accepted}  rejected ${run.counts.rejected}  dropped iterations ${run.counts.droppedIterations}`,
    `  http p50 ${fmt(run.httpMs.p50)} ms  p95 ${fmt(run.httpMs.p95)} ms  p99 ${fmt(run.httpMs.p99)} ms`,
    ...Object.entries(extra)
      .filter(([, v]) => typeof v !== 'object')
      .map(([k, v]) => `  ${k}: ${v}`),
    '',
  ].join('\n');
  return { stdout: text, [`${RAW_DIR}/${name}.run.json`]: JSON.stringify(run, null, 2) };
}
