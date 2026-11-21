/* AI assistant: three builders that turn plain words or a sample into a draft a person checks and saves.
   Rule from text (POST /v1/rules/from-text), field mapping for a source (POST /v1/sources/{id}/mapping/suggest)
   and runbook from free text (POST /v1/runbooks/parse). Nothing is saved without "Save" / "Accept". */
import { useEffect, useState } from 'react';

import Button from '@atlaskit/button/new';
import Lozenge from '@atlaskit/lozenge';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import SectionMessage from '@atlaskit/section-message';
import Select from '@atlaskit/select';
import Tabs, { Tab, TabList, TabPanel } from '@atlaskit/tabs';
import TextArea from '@atlaskit/textarea';
import Textfield from '@atlaskit/textfield';

import { AiDraft, ErrorRetry, RawToggle, Risk, Skeletons, useV1 } from '../ai';
import { useHerald } from '../store';
import { channelName, docTitle, go, Page, Severity } from '../ui';
import { v1, V1Error, type Condition, type DryRun, type Mapping, type RawEvent, type Recipient, type Rule, type RuleDraft, type Source, type V1Runbook, type V1Team } from '../v1';

type Option = { label: string; value: string };
const TABS = ['rule', 'mapping', 'runbook'] as const;
const UNAVAILABLE = 'The AI is not available right now. Try again in a minute, or build it by hand in the classic interface.';
const failure = (e: unknown) => (e instanceof V1Error && e.aiUnavailable ? UNAVAILABLE : e instanceof Error ? e.message : String(e));

function useTeams() {
  return useV1(() => v1<V1Team[]>('/v1/teams'), []);
}
function TeamPicker({ value, onChange, teams }: { value: string; onChange: (id: string) => void; teams: V1Team[] }) {
  const options: Option[] = teams.map((t) => ({ label: t.name, value: t.id }));
  return (
    <div className="pick-wide">
      <Select<Option> aria-label="Team" placeholder="Team" options={options} value={options.find((o) => o.value === value) ?? null}
        onChange={(o) => onChange(o?.value ?? '')} menuPosition="fixed" />
    </div>
  );
}

/* ---- rule ----------------------------------------------------------------------------- */
const OPS: Record<string, string> = { eq: 'is', neq: 'is not', gt: 'is above', gte: 'is at least', lt: 'is below', lte: 'is at most', in: 'is one of', not_in: 'is not one of', contains: 'contains', regex: 'matches', glob: 'looks like', exists: 'is present' };
export function conditionText(c: Condition | undefined): string {
  if (!c) return 'any event';
  if (c.conditions) return c.conditions.map(conditionText).join(c.op === 'or' ? ' or ' : ' and ') || 'any event';
  const v = Array.isArray(c.value) ? c.value.join(', ') : c.value === undefined || c.value === null ? '' : String(c.value);
  return `${c.field} ${OPS[c.operator ?? ''] ?? c.operator}${c.operator === 'exists' ? '' : ` ${v}`}`;
}
const recipients = (list: Recipient[] | undefined) =>
  (list ?? []).map((r) => (r.kind === 'oncall' ? 'whoever is on call' : r.kind === 'team' ? `team ${r.id}` : r.kind === 'role' ? `the ${r.id.replace(/_/g, ' ')}` : r.id)).join(', ') || 'nobody';

function DryRunLine({ result }: { result: DryRun | undefined }) {
  if (!result) return null;
  return (
    <Stack space="space.050">
      <Text>Dry-run on recent events: would have matched <b>{result.matched ?? 0}</b> of {result.evaluated ?? 0}, giving {result.alertsAfterDedup ?? 0} alert{result.alertsAfterDedup === 1 ? '' : 's'} after duplicates are merged.</Text>
      {!!result.notifications?.length && (
        <Text size="small" color="color.text.subtle">Would notify: {result.notifications.map((n) => `${n.recipient.displayName} (${n.kind === 'hard' ? 'must respond' : 'informed'}, ×${n.count})`).join(', ')}</Text>
      )}
    </Stack>
  );
}

function RuleBuilder() {
  const h = useHerald();
  const teams = useTeams();
  const [team, setTeam] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<RuleDraft | null>(null);
  const [json, setJson] = useState<string | null>(null);
  useEffect(() => { if (!team && teams.data?.length) setTeam(teams.data[0].id); }, [team, teams.data]);

  const ask = async () => {
    setBusy('draft'); setError(null); setDraft(null); setJson(null);
    try { setDraft(await v1<RuleDraft>('/v1/rules/from-text', { body: { text: text.trim(), teamId: team } })); } catch (e) { setError(failure(e)); }
    setBusy(null);
  };
  const edited = (): Rule | null => {
    if (json === null) return draft?.draft ?? null;
    try { return JSON.parse(json) as Rule; } catch { setError('The rule is not valid JSON. Fix it or press “Undo my edits”.'); return null; }
  };
  const retest = async () => {
    const rule = edited();
    if (!rule || !draft) return;
    setBusy('test'); setError(null);
    try { setDraft({ ...draft, draft: rule, dryRun: await v1<DryRun>('/v1/rules/dry-run', { body: { rule } }) }); } catch (e) { setError(failure(e)); }
    setBusy(null);
  };
  const save = async () => {
    const rule = edited();
    if (!rule) return;
    const { id: _id, version: _v, ...body } = rule;
    setBusy('save');
    const saved = await h.run(() => v1<Rule>('/v1/rules', { body }), `Rule “${rule.name}” saved. It works from the next event.`);
    setBusy(null);
    if (saved) { setDraft(null); setJson(null); setText(''); }
  };
  const rule = draft?.draft;
  return (
    <Stack space="space.200">
      <Text color="color.text.subtle">Describe who should hear about what. The AI writes the rule and tests it on recent events; you check and save it.</Text>
      {teams.error && !teams.data ? <ErrorRetry error={teams.error} onRetry={teams.reload} /> : <TeamPicker value={team} onChange={setTeam} teams={teams.data ?? []} />}
      <TextArea aria-label="What should alert whom?" minimumRows={3} value={text} onChange={(e) => setText(e.currentTarget.value)}
        placeholder="Anything critical from payments-api: SMS to Anna 08:00–20:00, otherwise email" />
      <Inline><Button appearance="primary" isLoading={busy === 'draft'} isDisabled={!text.trim() || !team} onClick={ask}>Draft rule</Button></Inline>
      {busy === 'draft' && <Skeletons lines={4} />}
      {error && <SectionMessage appearance="warning"><Text>{error}</Text></SectionMessage>}
      {rule && (
        <AiDraft title={rule.name} actions={(
          <>
            <Button appearance="primary" isLoading={busy === 'save'} onClick={save}>Save rule</Button>
            <Button onClick={() => setJson(json === null ? JSON.stringify(rule, null, 2) : null)}>{json === null ? 'Edit' : 'Undo my edits'}</Button>
            {json !== null && <Button isLoading={busy === 'test'} onClick={retest}>Test my edits</Button>}
            <Button appearance="subtle" onClick={() => { setDraft(null); setJson(null); }}>Reject</Button>
          </>
        )}>
          {draft.explanation && <Text>{draft.explanation}</Text>}
          <dl className="kv">
            <dt>When</dt><dd>{conditionText(rule.match)}</dd>
            {rule.priority && <><dt>Severity</dt><dd><Severity value={rule.priority} /></dd></>}
            <dt>Must respond</dt><dd>{recipients(rule.routing?.hard)}</dd>
            {!!rule.routing?.soft?.length && <><dt>Informed</dt><dd>{recipients(rule.routing.soft)}</dd></>}
            {rule.routing?.channelsBySeverity && <><dt>Channels</dt><dd>{Object.entries(rule.routing.channelsBySeverity).map(([s, c]) => `${s}: ${c.map(channelName).join(', ')}`).join(' · ')}</dd></>}
            {rule.presentation?.titleTemplate && <><dt>Alert title</dt><dd><code>{rule.presentation.titleTemplate}</code></dd></>}
          </dl>
          <DryRunLine result={draft.dryRun} />
          {json !== null
            ? <TextArea aria-label="Rule as JSON" className="mono" minimumRows={14} value={json} onChange={(e) => setJson(e.currentTarget.value)} />
            : <RawToggle id="rule-json" value={rule} label="Show the rule as JSON" />}
        </AiDraft>
      )}
    </Stack>
  );
}

/* ---- mapping -------------------------------------------------------------------------- */
const COMMON = ['severity', 'service', 'environment', 'timestamp', 'title', 'type', 'error_code'];

function MappingBuilder() {
  const h = useHerald();
  const sources = useV1(() => v1<Source[]>('/v1/sources'), []);
  const [source, setSource] = useState('');
  const [sample, setSample] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Mapping | null>(null);

  useEffect(() => {
    if (!source) return;
    let live = true;
    setDraft(null);
    v1<RawEvent[]>(`/v1/sources/${source}/sample-events?limit=1`).then(
      (events) => { if (live) setSample(events[0] ? JSON.stringify(events[0].payload, null, 2) : ''); },
      () => live && setSample(''),
    );
    return () => { live = false; };
  }, [source]);

  const suggest = async () => {
    let parsed: unknown;
    try { parsed = JSON.parse(sample); } catch { setError('The sample event is not valid JSON.'); return; }
    setBusy('suggest'); setError(null); setDraft(null);
    try { setDraft(await v1<Mapping>(`/v1/sources/${source}/mapping/suggest`, { body: { sample: parsed } })); } catch (e) { setError(failure(e)); }
    setBusy(null);
  };
  const accept = async () => {
    if (!draft) return;
    setBusy('save');
    const ok = await h.run(() => v1(`/v1/sources/${source}/mapping`, { method: 'PUT', body: { ...draft, status: 'accepted' } }), 'Mapping saved. New events from this source use it.');
    setBusy(null);
    if (ok !== undefined) { setDraft(null); sources.reload(); }
  };
  const options: Option[] = (sources.data ?? []).map((s) => ({ label: `${s.name}${s.mappingStatus === 'accepted' ? ' (mapped)' : ''}`, value: s.id }));
  return (
    <Stack space="space.200">
      <Text color="color.text.subtle">Every platform names its fields differently. Pick a source and a sample event; the AI proposes which field is the severity, the service, the title and so on.</Text>
      {sources.error && !sources.data ? <ErrorRetry error={sources.error} onRetry={sources.reload} /> : (
        <div className="pick-wide">
          <Select<Option> aria-label="Source" placeholder="Source" options={options} value={options.find((o) => o.value === source) ?? null}
            onChange={(o) => setSource(o?.value ?? '')} menuPosition="fixed" noOptionsMessage={() => 'No sources yet. Connect a platform first.'} />
        </div>
      )}
      {source && (
        <>
          <TextArea aria-label="Sample event (JSON)" className="mono" minimumRows={8} value={sample} onChange={(e) => setSample(e.currentTarget.value)}
            placeholder="Paste one event this source sends, as JSON" />
          <Inline><Button appearance="primary" isLoading={busy === 'suggest'} isDisabled={!sample.trim()} onClick={suggest}>Suggest mapping</Button></Inline>
        </>
      )}
      {busy === 'suggest' && <Skeletons lines={4} />}
      {error && <SectionMessage appearance="warning"><Text>{error}</Text></SectionMessage>}
      {draft && (
        <AiDraft title={draft.detectedSchema ? `Looks like ${draft.detectedSchema}` : 'Proposed mapping'}
          heuristic={/^AI unavailable/i.test(draft.explanation ?? '')} actions={(
          <>
            <Button appearance="primary" isLoading={busy === 'save'} onClick={accept}>Accept</Button>
            <Button appearance="subtle" onClick={() => setDraft(null)}>Reject</Button>
          </>
        )}>
          {draft.explanation && <Text>{draft.explanation.replace(/^AI unavailable, heuristic draft\.\s*/i, '')}</Text>}
          <div className="map-grid">
            <Text size="small" weight="semibold" color="color.text.subtle">Herald field</Text>
            <Text size="small" weight="semibold" color="color.text.subtle">Taken from (edit if wrong)</Text>
            {COMMON.map((f) => (
              <div key={f} style={{ display: 'contents' }}>
                <Text>{f.replace('_', ' ')}</Text>
                <Textfield isCompact aria-label={`Path for ${f}`} placeholder="not mapped" value={draft.fields?.[f] ?? ''}
                  onChange={(e) => {
                    const value = e.currentTarget.value;
                    setDraft((d) => (d ? { ...d, fields: { ...(d.fields ?? {}), [f]: value } } : d));
                  }} />
              </div>
            ))}
          </div>
          {draft.severityValueMap && Object.keys(draft.severityValueMap).length > 0 && (
            <Inline space="space.100" shouldWrap alignBlock="center">
              <Text size="small" color="color.text.subtle">Severity words:</Text>
              {Object.entries(draft.severityValueMap).map(([k, v]) => <Inline key={k} space="space.050" alignBlock="center"><code>{k}</code><Text>→</Text><Severity value={v} /></Inline>)}
            </Inline>
          )}
        </AiDraft>
      )}
    </Stack>
  );
}

/* ---- runbook -------------------------------------------------------------------------- */
/* A runbook's name: what the parser found, else the first heading or line of the text (never just "Runbook"). */
export function runbookName(name: string | null | undefined, text: string): string {
  const n = (name ?? '').trim();
  if (n && !/^(runbook|untitled)$/i.test(n)) return n;
  return docTitle(text) ?? (n || 'Untitled runbook');
}
function RunbookBuilder() {
  const h = useHerald();
  const teams = useTeams();
  const [team, setTeam] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<V1Runbook | null>(null);
  const parse = async () => {
    setBusy('parse'); setError(null); setDraft(null);
    try {
      const parsed = await v1<V1Runbook>('/v1/runbooks/parse', { body: { content: text, format: 'auto' } });
      setDraft({ ...parsed, name: runbookName(parsed.name, text) });
    } catch (e) { setError(failure(e)); }
    setBusy(null);
  };
  const save = async () => {
    if (!draft) return;
    setBusy('save');
    const ok = await h.run(() => v1('/v1/runbooks', { body: { ...draft, source: draft.source ?? text, ...(team ? { teamId: team } : {}) } }), `Runbook “${draft.name ?? 'Untitled'}” saved`);
    setBusy(null);
    if (ok) { setDraft(null); setText(''); }
  };
  const byAi = draft?.parsedBy === 'ai' || draft?.parsedBy === 'llm' || draft?.steps?.some((s) => s.parsedBy === 'ai' || s.parsedBy === 'llm');
  const guessed = !byAi && (draft?.parsedBy === 'heuristic' || draft?.steps?.some((s) => s.suggested));
  return (
    <Stack space="space.200">
      <Text color="color.text.subtle">Paste a runbook as you have it: a wiki page, a checklist, notes, YAML or a link. It becomes steps people can tick off and actions Herald can run.</Text>
      <TextArea aria-label="Runbook text" minimumRows={8} value={text} onChange={(e) => setText(e.currentTarget.value)}
        placeholder={'1. Check the counterparty service health page\n2. If it is down twice in a row, retry the workflow\n3. Still failing: page the settlements lead'} />
      <Inline><Button appearance="primary" isLoading={busy === 'parse'} isDisabled={!text.trim()} onClick={parse}>Turn into steps</Button></Inline>
      {busy === 'parse' && <Skeletons lines={4} />}
      {error && <SectionMessage appearance="warning"><Text>{error}</Text></SectionMessage>}
      {draft && (
        <AiDraft title={`${draft.steps?.length ?? 0} steps`} label={byAi ? 'AI suggestion' : guessed ? 'Heuristic draft' : 'Read from your text'} heuristic={!byAi}
          footnote={byAi ? undefined : guessed ? undefined : 'Read straight from the structure of your text; check it before you save.'}
          actions={(
            <>
              <Button appearance="primary" isLoading={busy === 'save'} onClick={save}>Save</Button>
              <Button appearance="subtle" onClick={() => setDraft(null)}>Reject</Button>
            </>
          )}>
          <Inline space="space.100" alignBlock="center" shouldWrap>
            <div style={{ width: 320 }}><Textfield aria-label="Runbook name" value={draft.name ?? ''} placeholder="Name" onChange={(e) => { const name = e.currentTarget.value; setDraft((d) => (d ? { ...d, name } : d)); }} /></div>
            {teams.data && <TeamPicker value={team} onChange={setTeam} teams={teams.data} />}
          </Inline>
          <Stack space="space.100">
            {(draft.steps ?? []).map((s, i) => {
              const risky = s.risk === 'high' || s.approval === 'required';
              return (
                <div key={s.id ?? i} className={`step${risky ? ' risky' : ''}`}>
                  <div className="step-num">{i + 1}</div>
                  <Stack space="space.050">
                    <Text>{s.text}</Text>
                    <Inline space="space.100" shouldWrap>
                      {s.action ? <Lozenge appearance="new">Automatic: {s.action.replace(/_/g, ' ')}</Lozenge> : <Lozenge>{s.url ? 'Open a link' : 'Done by hand'}</Lozenge>}
                      {s.action && <Risk value={s.risk} />}
                      {s.approval === 'required' && <Lozenge appearance="moved" isBold>Needs approval</Lozenge>}
                      {s.suggested && <Lozenge appearance="moved">Guessed: check it</Lozenge>}
                    </Inline>
                  </Stack>
                  <span />
                </div>
              );
            })}
          </Stack>
          <RawToggle id="runbook-json" value={draft} label="Show the steps as data" />
        </AiDraft>
      )}
    </Stack>
  );
}

export function Assistant({ tab }: { tab?: string }) {
  const index = Math.max(0, TABS.indexOf((tab ?? 'rule') as (typeof TABS)[number]));
  return (
    <Page title="AI assistant" lead="Plain words in, a draft out. The AI never saves anything: you read the draft, change what is wrong, and save it.">
      <Tabs id="assistant-tabs" selected={index} onChange={(i) => go(`/assistant/${TABS[i]}`)}>
        <TabList><Tab>Rule from text</Tab><Tab>Field mapping</Tab><Tab>Runbook from text</Tab></TabList>
        <TabPanel><div className="tab-body">{index === 0 && <RuleBuilder />}</div></TabPanel>
        <TabPanel><div className="tab-body">{index === 1 && <MappingBuilder />}</div></TabPanel>
        <TabPanel><div className="tab-body">{index === 2 && <RunbookBuilder />}</div></TabPanel>
      </Tabs>
    </Page>
  );
}
