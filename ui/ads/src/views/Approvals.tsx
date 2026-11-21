/* Approvals inbox: risky runbook actions and requests from AI agents that wait for a person (GET /v1/approvals). */
import { useState } from 'react';

import Button from '@atlaskit/button/new';
import EmptyState from '@atlaskit/empty-state';
import Lozenge from '@atlaskit/lozenge';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import Tabs, { Tab, TabList, TabPanel } from '@atlaskit/tabs';
import TextArea from '@atlaskit/textarea';

import { Countdown, ErrorRetry, RawToggle, Risk, Skeletons, useV1 } from '../ai';
import { useHerald } from '../store';
import { ago, openAlert, Page } from '../ui';
import { actorName, alertNumber, v1, type Approval } from '../v1';

const LIVE = ['approval.requested', 'approval.decided'] as const;
const STATUSES = ['pending', 'approved', 'rejected', 'expired'] as const;
const STATUS_LABEL: Record<string, string> = { pending: 'Waiting', approved: 'Approved', rejected: 'Rejected', expired: 'Expired' };

/* How many approvals wait for the current person; kept live for the badge in the side bar. */
export function useApprovalCount(): number {
  const { data } = useV1(() => v1<Approval[]>('/v1/approvals?status=pending'), [], { on: LIVE });
  return data?.length ?? 0;
}

const human = (key: string) => key.replace(/[_.]/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase();
const show = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v));

function ApprovalCard({ a, onDecided }: { a: Approval; onDecided: () => void }) {
  const h = useHerald();
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null);
  const pending = a.status === 'pending';
  const alertNo = alertNumber(a.alertId);
  const decide = async (decision: 'approve' | 'reject') => {
    setBusy(decision);
    const done = await h.run(
      () => v1<Approval>(`/v1/approvals/${a.id}/decision`, { body: { decision, ...(comment.trim() ? { comment: comment.trim() } : {}) } }),
      decision === 'approve' ? 'Approved. The action runs now.' : 'Rejected. The requester has been told.',
    );
    setBusy(null);
    if (done) onDecided();
  };
  const params = Object.entries(a.parameters ?? {});
  return (
    <article className={`approval${a.risk === 'high' ? ' risky' : ''}`} aria-label={`Approval for ${human(a.action)}`}>
      <Stack space="space.150">
        <Inline space="space.100" alignBlock="center" shouldWrap spread="space-between">
          <Inline space="space.100" alignBlock="center" shouldWrap>
            <Text weight="semibold" size="large">{human(a.action)}</Text>
            <Risk value={a.risk} />
            <Lozenge appearance={a.origin === 'agent_request' ? 'new' : 'default'}>{a.origin === 'agent_request' ? 'Asked by an AI agent' : 'Runbook step'}</Lozenge>
            {!pending && <Lozenge appearance={a.status === 'approved' ? 'success' : 'removed'}>{STATUS_LABEL[a.status]}</Lozenge>}
          </Inline>
          {pending && <Countdown until={a.expiresAt} prefix="Expires in" past="Expired" />}
        </Inline>
        <Text>
          <Text weight="medium">{actorName(a.requestedBy)}</Text> asks{a.createdAt ? ` · ${ago(a.createdAt)}` : ''}
          {a.shortId ? <Text color="color.text.subtle"> · or reply “APPROVE {a.shortId}” to the text message</Text> : null}
        </Text>
        {a.reason && <Text color="color.text.subtle">“{a.reason}”</Text>}
        {params.length > 0 && (
          <dl className="kv">
            {params.map(([k, v]) => <div key={k} style={{ display: 'contents' }}><dt>{human(k)}</dt><dd>{show(v)}</dd></div>)}
          </dl>
        )}
        <Inline space="space.200" alignBlock="center" shouldWrap>
          {alertNo && <Button appearance="subtle" onClick={() => openAlert(alertNo)}>Open alert A-{alertNo}</Button>}
          {a.approvers?.length ? <Text size="small" color="color.text.subtle">Who can approve: {a.approvers.map(actorName).join(', ')}</Text> : null}
        </Inline>
        {!pending && a.decidedBy && <Text size="small" color="color.text.subtle">{STATUS_LABEL[a.status]} by {actorName(a.decidedBy)}{a.comment ? `: “${a.comment}”` : ''}</Text>}
        {pending && (
          <Stack space="space.100">
            <TextArea aria-label="Comment for the requester" placeholder="Comment (optional): why you approve or reject" minimumRows={1} value={comment} onChange={(e) => setComment(e.currentTarget.value)} />
            <Inline space="space.100">
              <Button appearance={a.risk === 'high' ? 'warning' : 'primary'} isLoading={busy === 'approve'} isDisabled={busy !== null} onClick={() => decide('approve')}>
                {a.risk === 'high' ? 'Approve risky action' : 'Approve'}
              </Button>
              <Button isLoading={busy === 'reject'} isDisabled={busy !== null} onClick={() => decide('reject')}>Reject</Button>
            </Inline>
          </Stack>
        )}
        <RawToggle id={`raw-${a.id}`} value={a} label="Show the exact request" />
      </Stack>
    </article>
  );
}

function List({ status }: { status: (typeof STATUSES)[number] }) {
  const list = useV1(() => v1<Approval[]>(`/v1/approvals?status=${status}`), [status], { on: LIVE });
  if (list.loading && !list.data) return <Skeletons lines={4} height={48} />;
  if (list.error && !list.data) return <ErrorRetry error={list.error} onRetry={list.reload} />;
  const rows = list.data ?? [];
  if (!rows.length) {
    return (
      <EmptyState header={status === 'pending' ? 'Nothing waits for you' : `No ${STATUS_LABEL[status].toLowerCase()} approvals`}
        description={status === 'pending' ? 'When a runbook step is risky or an AI agent asks to do something, it appears here and you are told by text message.' : 'Decisions you make show up here.'} />
    );
  }
  return (
    <Stack space="space.200">
      {list.error && <ErrorRetry error={list.error} onRetry={list.reload} title="Showing the last list; refreshing failed" />}
      {rows.map((a) => <ApprovalCard key={a.id} a={a} onDecided={list.reload} />)}
    </Stack>
  );
}

export function Approvals() {
  const [tab, setTab] = useState(0);
  return (
    <Page title="Approvals" lead="Risky actions never run on their own. A person approves them here, or by replying to the text message.">
      <Tabs id="approval-tabs" selected={tab} onChange={setTab}>
        <TabList>{STATUSES.map((s) => <Tab key={s}>{STATUS_LABEL[s]}</Tab>)}</TabList>
        {STATUSES.map((s, i) => <TabPanel key={s}><div style={{ width: '100%', paddingTop: 16 }}>{tab === i && <List status={s} />}</div></TabPanel>)}
      </Tabs>
    </Page>
  );
}
