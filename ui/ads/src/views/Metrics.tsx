/* Health: the numbers behind "nothing is lost" (GET /v1/metrics/summary), refreshed every 5 seconds. */
import ErrorIcon from '@atlaskit/icon/core/status-error';
import SuccessIcon from '@atlaskit/icon/core/status-success';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import { token } from '@atlaskit/tokens';

import { ErrorRetry, Skeletons, useV1 } from '../ai';
import { channelName, Page } from '../ui';
import { v1, type Latency, type MetricsSummary } from '../v1';

const OUTCOME: Record<string, string> = {
  alerted_new: 'Became a new alert', deduplicated: 'Merged into an open alert', grouped: 'Grouped with others',
  unrouted: 'Matched no rule', invalid: 'Could not be read', auto_resolved: 'Closed an alert',
};
const n = (v: number | null | undefined, digits = 0) => (v === null || v === undefined ? '–' : v.toLocaleString(undefined, { maximumFractionDigits: digits }));
const ms = (v: number | null | undefined) => (v === null || v === undefined ? '–' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="tile metric">
      <Text size="small" weight="semibold" color="color.text.subtle">{label}</Text>
      <span className="metric-value">{value}</span>
      {note && <Text size="small" color="color.text.subtlest">{note}</Text>}
    </div>
  );
}

function Lost({ value }: { value: number | null | undefined }) {
  const unknown = value === null || value === undefined;
  const ok = value === 0;
  return (
    <div className={`tile metric hero ${ok ? 'good' : unknown ? '' : 'bad'}`}>
      <Text size="small" weight="semibold" color="color.text.subtle">Lost events</Text>
      <Inline space="space.100" alignBlock="center">
        {!unknown && (ok ? <SuccessIcon label="" color={token('color.icon.success')} /> : <ErrorIcon label="" color={token('color.icon.danger')} />)}
        <span className="metric-value">{n(value)}</span>
      </Inline>
      <Text size="small" color="color.text.subtle">{unknown ? 'Counters are not available right now' : ok ? 'Nothing lost: every event that came in was handled' : 'Some events are unaccounted for'}</Text>
    </div>
  );
}

function LatencyRow({ name, l }: { name: string; l: Latency }) {
  return (
    <div style={{ display: 'contents' }}>
      <dt>{name}</dt>
      <dd>{ms(l.p50)} typical · {ms(l.p99)} slowest 1% <Text size="small" color="color.text.subtlest">({n(l.count)} messages)</Text></dd>
    </div>
  );
}

export function Metrics() {
  const m = useV1(() => v1<MetricsSummary>('/v1/metrics/summary'), [], { every: 5000 });
  const d = m.data;
  return (
    <Page title="Health" lead={`How the service is doing${d?.windowMinutes ? ` over the last ${d.windowMinutes} minutes` : ''}. Updates every 5 seconds.`}>
      {m.loading && !d ? <Skeletons lines={4} height={72} /> : m.error && !d ? <ErrorRetry error={m.error} onRetry={m.reload} /> : d && (
        <Stack space="space.300">
          {m.error && <Text color="color.text.warning">Reconnecting… the numbers below are from the last answer.</Text>}
          <div className="tiles metrics">
            <Lost value={d.lost} />
            <Tile label="Events per second" value={n(d.eventsPerSecond, 1)} />
            <Tile label="Event to message, typical" value={ms(d.latencyP50Ms)} note="median (p50), all channels" />
            <Tile label="Event to message, slowest 1%" value={ms(d.latencyP99Ms)} note="p99, all channels" />
            <Tile label="Events received" value={n(d.eventsIngested)} />
            <Tile label="Messages delivered" value={n(d.notificationsDelivered)} />
            <Tile label="Waiting in queues" value={n(d.queueDepth)} note={d.queueDepth === null ? 'queue monitor not reachable' : undefined} />
            <Tile label="Parked for retry by hand" value={n(d.inDeadLetter)} note="dead-letter queue" />
          </div>
          <div className="tiles">
            <div className="panel">
              <Stack space="space.100">
                <Text weight="semibold">What happened to each event</Text>
                <dl className="kv">
                  {Object.entries(d.outcomes ?? {}).map(([k, v]) => <div key={k} style={{ display: 'contents' }}><dt>{OUTCOME[k] ?? k.replace(/_/g, ' ')}</dt><dd>{n(v)}</dd></div>)}
                </dl>
                {!Object.keys(d.outcomes ?? {}).length && <Text color="color.text.subtle">No events yet. Send one from the test lab.</Text>}
              </Stack>
            </div>
            <div className="panel">
              <Stack space="space.100">
                <Text weight="semibold">Speed per channel</Text>
                <dl className="kv">
                  {d.processingLatencyMs && <LatencyRow name="Alert created" l={d.processingLatencyMs} />}
                  {Object.entries(d.deliveryLatencyMs ?? {}).map(([c, l]) => <LatencyRow key={c} name={channelName(c)} l={l} />)}
                </dl>
                {!d.processingLatencyMs && !Object.keys(d.deliveryLatencyMs ?? {}).length && <Text color="color.text.subtle">No messages went out in this window.</Text>}
              </Stack>
            </div>
            {d.queues && (
              <div className="panel">
                <Stack space="space.100">
                  <Text weight="semibold">Queues</Text>
                  <dl className="kv">{Object.entries(d.queues).map(([q, v]) => <div key={q} style={{ display: 'contents' }}><dt className="mono">{q}</dt><dd>{n(v)}</dd></div>)}</dl>
                </Stack>
              </div>
            )}
          </div>
        </Stack>
      )}
    </Page>
  );
}
