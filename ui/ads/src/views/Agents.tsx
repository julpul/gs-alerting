/* Agents & MCP: personal access tokens, how to connect Claude Code, the tools an agent gets, and what agents did. */
import { useState } from 'react';

import Button from '@atlaskit/button/new';
import { CodeBlock } from '@atlaskit/code';
import EmptyState from '@atlaskit/empty-state';
import Heading from '@atlaskit/heading';
import Lozenge from '@atlaskit/lozenge';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import SectionMessage from '@atlaskit/section-message';
import Textfield from '@atlaskit/textfield';

import { api, type AuditEntry } from '../api';
import { CopyButton, ErrorRetry, Skeletons, useV1 } from '../ai';
import { useHerald } from '../store';
import { Table } from '../Table';
import { ago, clock, Page } from '../ui';
import { v1, type PersonalToken, type PersonalTokenCreated } from '../v1';

/* From backend/app/mcp/README.md (the server's own list of tools). */
export const MCP_TOOLS: [string, string][] = [
  ['list_alerts', 'Lists alerts you can see, by status or team.'],
  ['get_alert', 'One alert with its runbook, latest AI suggestion, deliveries and timeline.'],
  ['ack_alert', 'Acknowledges an alert as you (needs member or higher on the team).'],
  ['comment_alert', 'Adds a comment to the alert timeline.'],
  ['raise_alert', 'Raises a typed alert through the same pipeline as POST /v1/alerts/raise.'],
  ['request_approval', 'Asks a person to approve an action and waits (up to 5 minutes) for the answer.'],
  ['get_approval_status', 'Checks whether an approval was granted, rejected or expired.'],
  ['run_runbook_step', 'Runs a low-risk, trusted step; anything riskier becomes an approval request.'],
  ['propose_rule', 'Drafts a rule from plain words (never saved).'],
  ['test_rule', 'Dry-runs a rule against recent events (never saved).'],
  ['propose_schema_mapping', 'Drafts how a source\'s fields map to severity, service, title… (never saved).'],
  ['propose_runbook', 'Turns runbook text into structured steps (never saved).'],
  ['get_integration_guide', 'A guide with curl and Python examples, plus the API contract as a resource.'],
];

function NewToken({ onCreated }: { onCreated: (t: PersonalTokenCreated) => void }) {
  const h = useHerald();
  const [name, setName] = useState('claude-code');
  const [busy, setBusy] = useState(false);
  const create = async () => {
    setBusy(true);
    const t = await h.run(() => v1<PersonalTokenCreated>('/v1/tokens', { body: { name: name.trim() } }), 'Token created. Copy it now: it is shown only once.');
    setBusy(false);
    if (t) onCreated(t);
  };
  return (
    <form onSubmit={(e) => { e.preventDefault(); void create(); }}>
      <Inline space="space.100" alignBlock="center" shouldWrap>
        <div style={{ width: 260 }}><Textfield aria-label="Token name" placeholder="What will use it, e.g. claude-code" value={name} maxLength={120} onChange={(e) => setName(e.currentTarget.value)} /></div>
        <Button type="submit" appearance="primary" isLoading={busy} isDisabled={!name.trim()}>Create token</Button>
      </Inline>
    </form>
  );
}

function AgentActivity() {
  const { data, error, loading } = useV1(async () => {
    try { return await api<AuditEntry[]>('/api/audit?limit=200'); } catch { return null; }   // Herald adapters not deployed: leave it out
  }, []);
  if (loading) return <Skeletons lines={3} />;
  if (error || data === null || data === undefined) return null;
  const agentic = data.filter((e) => /^agent[:\s]/i.test(e.actor) || (e.detail as { actor?: { type?: string; kind?: string } })?.actor?.type === 'agent' || 'onBehalfOf' in (e.detail ?? {})).slice(0, 20);
  return (
    <Stack space="space.150">
      <Heading size="small" as="h2">What agents did recently</Heading>
      {agentic.length ? (
        <Table head={{ cells: [{ key: 'at', content: 'When', width: 16 }, { key: 'who', content: 'Agent' }, { key: 'what', content: 'Did' }, { key: 'target', content: 'On' }] }}
          rows={agentic.map((e) => ({ key: String(e.id), cells: [
            { key: 'at', content: <span title={clock(e.at)}>{ago(e.at)}</span> },
            { key: 'who', content: e.actor },
            { key: 'what', content: e.action.replace(/[._]/g, ' ') },
            { key: 'target', content: e.target },
          ] }))} />
      ) : <Text color="color.text.subtle">No agent has done anything yet. Once one is connected, its actions appear here and in each alert's timeline as “agent … on behalf of …”.</Text>}
    </Stack>
  );
}

export function Agents() {
  const h = useHerald();
  const tokens = useV1(() => v1<PersonalToken[]>('/v1/tokens'), []);
  const [created, setCreated] = useState<PersonalTokenCreated | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const origin = location.origin;
  const snippet = `claude mcp add --transport http alerting ${origin}/mcp --header "Authorization: Bearer ${created?.token ?? '<PAT>'}"`;
  const revoke = async (t: PersonalToken) => {
    setRevoking(t.id);
    await h.run(() => v1(`/v1/tokens/${t.id}`, { method: 'DELETE' }), `Token “${t.name}” revoked. It stops working now.`);
    setRevoking(null);
    if (created?.id === t.id) setCreated(null);
    tokens.reload();
  };
  return (
    <Page title="Agents & MCP" lead="Coding agents such as Claude Code connect to Herald over MCP. They act with your permissions, every write is recorded, and risky actions wait for a person.">
      <Stack space="space.400">
        <Stack space="space.150">
          <Heading size="small" as="h2">1. Create a personal access token</Heading>
          <NewToken onCreated={(t) => { setCreated(t); tokens.reload(); }} />
          {created && (
            <SectionMessage appearance="success" title={`Your token “${created.name}”`}>
              <Stack space="space.100">
                <Text>Copy it now. Herald stores only a fingerprint, so it cannot show it again.</Text>
                <Inline space="space.100" alignBlock="center" shouldWrap><code className="secret">{created.token}</code><CopyButton text={created.token} label="Copy token" /></Inline>
              </Stack>
            </SectionMessage>
          )}
          {tokens.loading && !tokens.data ? <Skeletons lines={2} /> : tokens.error && !tokens.data ? <ErrorRetry error={tokens.error} onRetry={tokens.reload} /> : (
            <Table
              head={{ cells: [{ key: 'name', content: 'Name' }, { key: 'prefix', content: 'Starts with' }, { key: 'created', content: 'Created' }, { key: 'used', content: 'Last used' }, { key: 'x', content: '' }] }}
              rows={(tokens.data ?? []).map((t) => ({ key: t.id, cells: [
                { key: 'name', content: <Text weight="medium">{t.name}</Text> },
                { key: 'prefix', content: <code>{t.prefix}…</code> },
                { key: 'created', content: ago(t.createdAt) },
                { key: 'used', content: t.lastUsedAt ? ago(t.lastUsedAt) : 'Never' },
                { key: 'x', content: <Button appearance="subtle" isLoading={revoking === t.id} onClick={() => revoke(t)}>Revoke</Button> },
              ] }))}
              emptyView={<EmptyState header="No tokens yet" description="Create one above, then use it in the command below." />}
            />
          )}
        </Stack>

        <Stack space="space.150">
          <Heading size="small" as="h2">2. Connect Claude Code</Heading>
          <Text color="color.text.subtle">Run this in a terminal. Any MCP client that speaks streamable HTTP works the same way.</Text>
          <CodeBlock language="bash" showLineNumbers={false} text={snippet} />
          <Inline><CopyButton text={snippet} label="Copy command" /></Inline>
          {!created && <Text size="small" color="color.text.subtle">Replace &lt;PAT&gt; with your token, or create one above and the command fills itself in.</Text>}
        </Stack>

        <Stack space="space.150">
          <Heading size="small" as="h2">What an agent can do</Heading>
          <dl className="kv tools">
            {MCP_TOOLS.map(([name, text]) => (
              <div key={name} style={{ display: 'contents' }}>
                <dt><code>{name}</code>{name.startsWith('propose') || name === 'test_rule' ? <> <Lozenge appearance="new">draft only</Lozenge></> : null}</dt>
                <dd>{text}</dd>
              </div>
            ))}
          </dl>
        </Stack>

        <AgentActivity />
      </Stack>
    </Page>
  );
}
