/* The AI copilot: chat over POST /v1/agent/chat (streamed). It reads alerts, past incidents and rules with the
   person's own permissions and can only produce drafts and approval requests; saving is always a click here. */
import { useEffect, useRef, useState } from 'react';

import Button from '@atlaskit/button/new';
import Lozenge from '@atlaskit/lozenge';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import SectionMessage from '@atlaskit/section-message';
import TextArea from '@atlaskit/textarea';

import { AiDraft, AiLabel, RawToggle } from '../ai';
import { runbookName } from './Assistant';
import { useHerald } from '../store';
import { go, Page } from '../ui';
import { chat, v1, V1Error, type ChatEvent, type Rule } from '../v1';

type Tool = { name: string; args: Record<string, unknown>; summary?: string };
type Draft = Extract<ChatEvent, { event: 'draft' }>['data'] & { args?: Record<string, unknown>; done?: 'saved' | 'rejected' };
type Turn =
  | { role: 'user'; text: string }
  | { role: 'assistant'; text: string; tools: Tool[]; drafts: Draft[]; error?: string; busy: boolean };

const TOOL_LABEL: Record<string, string> = {
  list_alerts: 'Looked at alerts', get_alert: 'Read the alert', ack_alert: 'Acknowledged the alert', comment_alert: 'Wrote a comment',
  propose_rule: 'Drafted a rule', test_rule: 'Ran a dry-run', propose_schema_mapping: 'Drafted a field mapping',
  propose_runbook: 'Drafted a runbook', request_approval: 'Asked a person for approval', get_approval_status: 'Checked an approval',
  run_runbook_step: 'Ran a runbook step', get_integration_guide: 'Read the integration guide',
};
const UNAVAILABLE = 'The AI assistant is not available right now. Everything else in Herald works; try again in a minute.';

function ToolChip({ tool }: { tool: Tool }) {
  return (
    <details className="tool-chip">
      <summary>{TOOL_LABEL[tool.name] ?? tool.name.replace(/_/g, ' ')}{tool.summary === undefined ? '…' : ''}</summary>
      <div className="mono">{JSON.stringify(tool.args)}</div>
      {tool.summary && <div className="mono subtle">{tool.summary}</div>}
    </details>
  );
}

function DraftCard({ draft, onDone }: { draft: Draft; onDone: (how: 'saved' | 'rejected') => void }) {
  const h = useHerald();
  const [busy, setBusy] = useState(false);
  const data = draft.data as Record<string, any>;
  const save = async () => {
    setBusy(true);
    let ok: unknown;
    if (draft.kind === 'rule') {
      const { id: _id, version: _v, ...rule } = (data.draft ?? {}) as Rule;
      ok = await h.run(() => v1('/v1/rules', { body: rule }), `Rule “${rule.name}” saved`);
    } else if (draft.kind === 'runbook') {
      const name = runbookName(data.name, String(data.source ?? (data.steps?.[0]?.text ?? '')));
      ok = await h.run(() => v1('/v1/runbooks', { body: { name, format: data.format, steps: data.steps, attached: data.attached, ...(data.source ? { source: data.source } : {}) } }), `Runbook “${name}” saved`);
    } else if (draft.kind === 'mapping' && draft.args?.source_id) {
      const { status: _s, ...mapping } = data;
      ok = await h.run(() => v1(`/v1/sources/${draft.args?.source_id}/mapping`, { method: 'PUT', body: { ...mapping, status: 'accepted' } }), 'Mapping saved');
    }
    setBusy(false);
    if (ok !== undefined) onDone('saved');
  };
  const title = draft.kind === 'rule' ? `Rule: ${data.draft?.name ?? 'draft'}`
    : draft.kind === 'runbook' ? `Runbook: ${runbookName(data.name, String(data.source ?? data.steps?.[0]?.text ?? ''))}`
      : draft.kind === 'mapping' ? 'Field mapping for a source' : `Approval request: ${String(data.action ?? '').replace(/_/g, ' ')}`;
  const canSave = draft.kind === 'rule' || draft.kind === 'runbook' || (draft.kind === 'mapping' && !!draft.args?.source_id);
  return (
    <AiDraft title={title} label={draft.kind === 'approval' ? 'Sent for approval' : 'AI draft'}
      footnote={draft.kind === 'approval' ? 'A person has to approve this before anything happens.' : undefined}
      actions={draft.done ? <Lozenge appearance={draft.done === 'saved' ? 'success' : 'removed'}>{draft.done === 'saved' ? 'Saved' : 'Rejected'}</Lozenge> : (
        draft.kind === 'approval' ? <Button onClick={() => go('/approvals')}>Open approvals</Button> : (
          <>
            {canSave && <Button appearance="primary" isLoading={busy} onClick={save}>{draft.kind === 'rule' ? 'Save rule' : draft.kind === 'runbook' ? 'Save runbook' : 'Accept mapping'}</Button>}
            <Button onClick={() => go(draft.kind === 'rule' ? '/assistant/rule' : draft.kind === 'runbook' ? '/assistant/runbook' : '/assistant/mapping')}>Edit in AI assistant</Button>
            <Button appearance="subtle" onClick={() => onDone('rejected')}>Reject</Button>
          </>
        ))}>
      {typeof data.explanation === 'string' && <Text>{data.explanation}</Text>}
      {draft.kind === 'runbook' && Array.isArray(data.steps) && (
        <ol className="ladder">{data.steps.map((st: { text?: string; action?: string | null }, i: number) => <li key={i}><Text>{st.text}</Text>{st.action ? <Text size="small" color="color.text.subtle"> · runs {st.action.replace(/_/g, ' ')}</Text> : null}</li>)}</ol>
      )}
      {data.dryRun && <Text size="small" color="color.text.subtle">Dry-run: would have matched {data.dryRun.matched ?? 0} of {data.dryRun.evaluated ?? 0} recent events ({data.dryRun.alertsAfterDedup ?? 0} alerts).</Text>}
      <RawToggle id={`draft-${title}`} value={data} label="Show the draft" />
    </AiDraft>
  );
}

export function CopilotChat({ alertId, compact = false }: { alertId?: string; compact?: boolean }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState('');
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement | null>(null);
  const busy = turns.some((t) => t.role === 'assistant' && t.busy);
  useEffect(() => () => abort.current?.abort(), []);
  useEffect(() => { end.current?.scrollIntoView({ block: 'nearest' }); }, [turns]);

  const update = (fn: (t: Extract<Turn, { role: 'assistant' }>) => Extract<Turn, { role: 'assistant' }>) =>
    setTurns((all) => all.map((t, i) => (i === all.length - 1 && t.role === 'assistant' ? fn(t) : t)));

  const ask = async (question: string) => {
    const q = question.trim();
    if (!q || busy) return;
    setText('');
    const history = [...turns, { role: 'user' as const, text: q }];
    setTurns([...history, { role: 'assistant', text: '', tools: [], drafts: [], busy: true }]);
    const ctl = new AbortController();
    abort.current = ctl;
    let lastArgs: Record<string, unknown> | undefined;
    try {
      await chat(history.map((t) => ({ role: t.role, content: t.text })), alertId ? { alertId } : {}, (e) => {
        if (e.event === 'token') update((t) => ({ ...t, text: t.text + e.data.text }));
        else if (e.event === 'tool_call') { lastArgs = e.data.args; update((t) => ({ ...t, tools: [...t.tools, { name: e.data.name, args: e.data.args }] })); }
        else if (e.event === 'tool_result') update((t) => ({ ...t, tools: t.tools.map((tool, i) => (i === t.tools.length - 1 ? { ...tool, summary: e.data.summary } : tool)) }));
        else if (e.event === 'draft') update((t) => ({ ...t, drafts: [...t.drafts, { ...e.data, args: lastArgs }] }));
        else if (e.event === 'error') update((t) => ({ ...t, error: e.data.code === 'ai_unavailable' ? UNAVAILABLE : e.data.message }));
      }, ctl.signal);
    } catch (error) {
      if (!ctl.signal.aborted) update((t) => ({ ...t, error: error instanceof V1Error && error.aiUnavailable ? UNAVAILABLE : error instanceof Error ? error.message : String(error) }));
    }
    update((t) => ({ ...t, busy: false }));
  };

  const starters = alertId
    ? ['Why is this alerting?', 'What fixed this last time?', 'Which runbook step should I run first?']
    : ['What is open for my team right now?', 'Create a rule: critical from payments-api → Anna by SMS 8–20', 'Turn this into a runbook: restart the service, then check the error rate'];

  return (
    <div className={`copilot${compact ? ' compact' : ''}`}>
      <div className="copilot-log" aria-live="polite">
        {!turns.length && (
          <Stack space="space.150">
            <Text color="color.text.subtle">Ask about {alertId ? `alert ${alertId}` : 'alerts, past incidents, rules or runbooks'}. Try:</Text>
            <Inline space="space.100" shouldWrap>
              {starters.map((s) => <button key={s} type="button" className="starter" onClick={() => void ask(s)}>{s}</button>)}
            </Inline>
          </Stack>
        )}
        {turns.map((t, i) => (t.role === 'user' ? (
          <div key={i} className="bubble user"><Text>{t.text}</Text></div>
        ) : (
          <div key={i} className="bubble assistant">
            <Stack space="space.100">
              <AiLabel text="Copilot" />
              {!!t.tools.length && <div className="tool-chips">{t.tools.map((tool, j) => <ToolChip key={j} tool={tool} />)}</div>}
              {t.text ? <div className="answer">{t.text}</div> : t.busy && !t.error ? <Text color="color.text.subtle">Thinking…</Text> : null}
              {t.drafts.map((d, j) => (
                <DraftCard key={j} draft={d} onDone={(how) => setTurns((all) => all.map((x, k) => (k === i && x.role === 'assistant' ? { ...x, drafts: x.drafts.map((y, m) => (m === j ? { ...y, done: how } : y)) } : x)))} />
              ))}
              {t.error && <SectionMessage appearance="warning"><Text>{t.error}</Text></SectionMessage>}
            </Stack>
          </div>
        )))}
        <div ref={end} />
      </div>
      <form className="copilot-input" onSubmit={(e) => { e.preventDefault(); void ask(text); }}>
        <TextArea aria-label="Ask the copilot" placeholder="Ask the copilot" minimumRows={compact ? 2 : 3} value={text}
          onChange={(e) => setText(e.currentTarget.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void ask(text); } }} />
        <Inline space="space.100" alignBlock="center" spread="space-between">
          <Text size="small" color="color.text.subtlest">The copilot only drafts and asks; you decide what is saved.</Text>
          <Inline space="space.100">
            {busy && <Button appearance="subtle" onClick={() => abort.current?.abort()}>Stop</Button>}
            {!busy && !!turns.length && <Button appearance="subtle" onClick={() => setTurns([])}>New conversation</Button>}
            <Button type="submit" appearance="primary" isDisabled={!text.trim() || busy}>Ask</Button>
          </Inline>
        </Inline>
      </form>
    </div>
  );
}

export function Copilot({ alertId }: { alertId?: string }) {
  return (
    <Page title="Copilot" lead="Ask in plain words. The copilot reads with your permissions, and it only drafts rules, mappings and runbooks or asks a person to approve; it never changes anything by itself.">
      {alertId && <Text color="color.text.subtle">Talking about alert {alertId}.</Text>}
      <CopilotChat alertId={alertId} />
    </Page>
  );
}
