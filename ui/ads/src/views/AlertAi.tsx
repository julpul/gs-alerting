/* What the alert view adds from /v1: the AI suggestion, the suspicious-content warning, the escalation
   countdown and resolving with an AI-drafted note that goes into the team's knowledge. */
import { useEffect, useState } from 'react';

import Button from '@atlaskit/button/new';
import Heading from '@atlaskit/heading';
import Lozenge from '@atlaskit/lozenge';
import Modal, { ModalBody, ModalFooter, ModalHeader, ModalTitle } from '@atlaskit/modal-dialog';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import SectionMessage from '@atlaskit/section-message';
import TextArea from '@atlaskit/textarea';

import { AiDraft, AiLabel, Countdown, percent, Skeletons, useNow, useV1 } from '../ai';
import { useHerald } from '../store';
import { openAlert } from '../ui';
import { alertNumber, alertRef, v1, V1Error, type NoteDraft, type Suggestion, type V1Alert } from '../v1';

const ALERT_EVENTS = ['alert.updated', 'alert.acked', 'alert.escalated', 'alert.resolved'] as const;
const NEXT_LEVEL: Record<number, string> = { 1: 'Domain Root', 2: 'Master' };

export function useV1Alert(id: number) {
  return useV1(() => v1<V1Alert>(`/v1/alerts/${alertRef(id)}`), [id], { on: ALERT_EVENTS });
}
export const isSuspicious = (a: V1Alert | undefined) => !!a?.flags?.includes('suspicious_content');

export function SuspiciousBanner() {
  return (
    <SectionMessage appearance="warning" title="Suspicious content">
      <Text>This event contains text that looks like instructions. AI actions are disabled for it.</Text>
    </SectionMessage>
  );
}

/* "Escalates to Domain Root in 3:12" while nobody has acknowledged the alert. */
export function EscalationLine({ alert }: { alert: V1Alert }) {
  if (!alert.escalatesAt || !['open', 'escalated'].includes(alert.status)) return null;
  const next = NEXT_LEVEL[alert.escalationLevel ?? 1];
  if (!next) return null;
  return (
    <div className="escalation" role="status">
      <Lozenge appearance="moved" isBold>Not acknowledged</Lozenge>
      <Countdown until={alert.escalatesAt} prefix={`Escalates to ${next} in`} past={`Escalating to ${next} now…`} />
    </div>
  );
}

function AlertLink({ id }: { id: string }) {
  const n = alertNumber(id);
  return n ? <a href={`#/alerts?alert=${n}`} onClick={(e) => { e.preventDefault(); openAlert(n); }}>{id}</a> : <Text>{id}</Text>;
}

export function SuggestionPanel({ alert, disabled }: { alert: V1Alert; disabled: boolean }) {
  const h = useHerald();
  const ref = alertRef(alert.shortId ?? alert.id);
  const s = useV1(async () => {
    try { return await v1<Suggestion>(`/v1/alerts/${ref}/suggestion`); } catch (e) {
      if (e instanceof V1Error && e.status === 404) return null;   // not ready yet: suggestion.ready tells when
      throw e;
    }
  }, [ref], { on: ['suggestion.ready'] });
  const [busy, setBusy] = useState<string | null>(null);
  const started = useState(() => Date.now())[0];
  const now = useNow(5000);

  const decide = async (decision: 'approve' | 'reject', sg: Suggestion) => {
    setBusy(decision);
    await h.run(() => v1(`/v1/alerts/${ref}/suggestion/${sg.id}/decision`, { body: { decision } }), decision === 'approve' ? 'Suggestion accepted' : 'Suggestion rejected');
    setBusy(null);
    s.reload();
  };

  if (disabled) {
    return (
      <Stack space="space.100">
        <Inline space="space.100" alignBlock="center"><Heading size="small" as="h3">AI suggestion</Heading><AiLabel text="AI" /></Inline>
        <Text color="color.text.subtle">AI is turned off for this alert because its content looks like instructions.</Text>
      </Stack>
    );
  }
  if (s.error && s.data === undefined) {
    return <SectionMessage appearance="warning" title="AI suggestion unavailable"><Text>{s.error}</Text></SectionMessage>;
  }
  if (!s.data) {
    const slow = now - started > 60000;
    return (
      <section className="ai-draft" aria-busy="true">
        <Stack space="space.150">
          <Inline space="space.100" alignBlock="center"><AiLabel /><Text weight="semibold">Analysing… (usually a few seconds)</Text></Inline>
          <Skeletons lines={3} />
          {slow && <Text size="small" color="color.text.subtle">Still working. The AI service may be busy; this updates by itself.</Text>}
        </Stack>
      </section>
    );
  }
  const sg = s.data;
  const step = alert.runbook?.steps?.find((st) => st.id === sg.nextStepId);
  const decided = sg.status !== 'draft';
  return (
    <AiDraft
      title={sg.aiUnavailable ? 'AI is unavailable: what we know from past incidents' : 'Suggested fix'}
      label={sg.aiUnavailable ? 'Past incidents' : 'AI suggestion'}
      footnote={sg.aiUnavailable ? 'The AI model did not answer, so only matches with past incidents are shown.' : undefined}
      actions={decided ? <Lozenge appearance={sg.status === 'accepted' ? 'success' : 'removed'}>{sg.status === 'accepted' ? 'You accepted this' : 'You rejected this'}</Lozenge> : sg.aiUnavailable ? null : (
        <>
          <Button appearance="primary" isLoading={busy === 'approve'} onClick={() => decide('approve', sg)}>Accept</Button>
          <Button isLoading={busy === 'reject'} onClick={() => decide('reject', sg)}>Reject</Button>
        </>
      )}
    >
      <Stack space="space.150">
        {sg.diagnosis && <Text>{sg.diagnosis}</Text>}
        {sg.nextStep && (
          <Stack space="space.050">
            <Text size="small" weight="semibold" color="color.text.subtle">Next step</Text>
            <Text>{sg.nextStep}</Text>
            {step && (
              <a href="#alert-runbook" onClick={(e) => { e.preventDefault(); document.getElementById('alert-runbook')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }}>
                Runbook step {step.order ?? ''}: {step.text}
              </a>
            )}
          </Stack>
        )}
        {sg.confidence !== undefined && !sg.aiUnavailable && <Text size="small" color="color.text.subtle">Confidence: {percent(sg.confidence)}{sg.reusedFrom ? ` · same as ${sg.reusedFrom}` : ''}</Text>}
        {!!sg.fixedBefore?.length && (
          <Stack space="space.075">
            <Text size="small" weight="semibold" color="color.text.subtle">Fixed before</Text>
            {sg.fixedBefore.map((f) => (
              <div key={f.alertId} className="memory"><AlertLink id={f.alertId} />{f.resolutionNote && <Text> “{f.resolutionNote}”</Text>}</div>
            ))}
          </Stack>
        )}
        {!!sg.similarIncidents?.length && (
          <Stack space="space.075">
            <Text size="small" weight="semibold" color="color.text.subtle">Similar incidents</Text>
            {sg.similarIncidents.map((f) => (
              <div key={f.alertId} className="memory">
                <AlertLink id={f.alertId} /> <Text>{f.title}</Text>
                {f.similarity !== undefined && <Text size="small" color="color.text.subtlest"> · {percent(f.similarity)} alike</Text>}
                {f.resolutionNote && <div><Text color="color.text.subtle">Fixed by: “{f.resolutionNote}”</Text></div>}
              </div>
            ))}
          </Stack>
        )}
        {!!sg.sources?.length && (
          <Stack space="space.050">
            <Text size="small" weight="semibold" color="color.text.subtle">Based on</Text>
            <Inline space="space.100" shouldWrap>
              {sg.sources.map((src, i) => (src.url && /^https?:\/\//.test(src.url)
                ? <a key={i} href={src.url} target="_blank" rel="noreferrer noopener">{src.title ?? src.kind}</a>
                : <Lozenge key={i}>{src.title ?? src.kind}</Lozenge>))}
            </Inline>
          </Stack>
        )}
      </Stack>
    </AiDraft>
  );
}

/* "How did you fix it?" prefilled with an AI draft the person edits; saved to knowledge on resolve. */
export function ResolveDialog({ id, suspicious, onClose }: { id: number; suspicious: boolean; onClose: () => void }) {
  const h = useHerald();
  const [note, setNote] = useState('');
  const [draft, setDraft] = useState<NoteDraft | null>(null);
  const [state, setState] = useState<'loading' | 'ready' | 'failed'>(suspicious ? 'ready' : 'loading');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (suspicious) return;
    let live = true;
    v1<NoteDraft>(`/v1/alerts/${alertRef(id)}/resolution-note-draft`).then(
      (d) => { if (live) { setDraft(d); setNote(d.note); setState('ready'); } },
      () => live && setState('failed'),
    );
    return () => { live = false; };
  }, [id, suspicious]);
  const resolve = async () => {
    setBusy(true);
    const ok = await h.run(() => v1(`/v1/alerts/${alertRef(id)}/resolve`, { body: note.trim() ? { resolutionNote: note.trim() } : {} }), `A-${id} resolved${note.trim() ? '. The note is saved for next time.' : ''}`);
    setBusy(false);
    if (ok !== undefined) onClose();
  };
  return (
    <Modal onClose={onClose} width="medium">
      <ModalHeader hasCloseButton><ModalTitle>How did you fix it?</ModalTitle></ModalHeader>
      <ModalBody>
        <Stack space="space.150">
          <Text color="color.text.subtle">Your note is saved to the team's knowledge, so the next similar alert shows it under “Fixed before”.</Text>
          {state === 'loading' && <Stack space="space.100"><AiLabel text="AI draft" /><Text>Drafting a note from the timeline…</Text><Skeletons lines={3} /></Stack>}
          {state !== 'loading' && draft && (
            <Inline space="space.100" alignBlock="center">
              <AiLabel text={draft.aiUnavailable ? 'Draft from the timeline' : 'AI draft'} />
              <Text size="small" color="color.text.subtle">Edit it, or clear it and write your own.</Text>
            </Inline>
          )}
          {state === 'failed' && <Text size="small" color="color.text.subtle">The AI draft did not load. Write the note yourself.</Text>}
          {suspicious && <Text size="small" color="color.text.subtle">No AI draft: this alert contains suspicious content.</Text>}
          {state !== 'loading' && (
            <TextArea aria-label="Resolution note" minimumRows={5} value={note} placeholder="What was wrong, and what you did" onChange={(e) => setNote(e.currentTarget.value)} />
          )}
        </Stack>
      </ModalBody>
      <ModalFooter>
        {draft && note && <Button appearance="subtle" onClick={() => setNote('')}>Reject draft</Button>}
        <Button appearance="subtle" onClick={onClose}>Cancel</Button>
        <Button appearance="primary" isLoading={busy} isDisabled={state === 'loading'} onClick={resolve}>{note.trim() ? 'Accept note and resolve' : 'Resolve without a note'}</Button>
      </ModalFooter>
    </Modal>
  );
}
