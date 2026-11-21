/* Channel preferences, editable.
   - My notification preferences: GET/PUT /v1/preferences/me (channels per severity, working hours).
   - Team channel policy: the team's escalation policy per severity (GET /v1/escalation-policies/effective,
     PUT /v1/escalation-policies with scope team, DELETE to fall back to the inherited policy).
     Domain Roots of the team's domain and the Master may change it; anyone else reads it. */
import { useEffect, useMemo, useState } from 'react';

import Button from '@atlaskit/button/new';
import Heading from '@atlaskit/heading';
import Lozenge from '@atlaskit/lozenge';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import SectionMessage from '@atlaskit/section-message';
import Select from '@atlaskit/select';
import Textfield from '@atlaskit/textfield';
import Toggle from '@atlaskit/toggle';

import { ErrorRetry, Skeletons, useV1 } from '../ai';
import { useHerald } from '../store';
import { channelName, Page, Severity } from '../ui';
import { v1, V1Error, type EffectivePolicy, type EscalationLevel, type V1Team } from '../v1';

type Option = { label: string; value: string };
export type ChannelPreferences = { bySeverity: Record<string, string[]>; workingHours?: { from: string; to: string; timezone: string } | null };

const SEVERITIES = ['critical', 'error', 'warning', 'info'] as const;
const CHANNELS = ['phone', 'sms', 'email', 'ui'] as const;
const CHANNEL_OPTIONS: Option[] = CHANNELS.map((c) => ({ value: c, label: channelName(c) }));
const ZONES = ['UTC', 'Europe/Warsaw', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney'];
const TARGET: Record<string, string> = { oncall: 'On-call person', domain_root: 'Domain Root', master: 'Master', custom: 'Named people' };
const ORIGIN: Record<string, string> = { org: 'From organisation', domain: 'From domain', team: 'Set for this team', rule: 'From rule' };

function Channels({ label, value, onChange, disabled = false }: { label: string; value: string[]; onChange: (v: string[]) => void; disabled?: boolean }) {
  return (
    <div className="pick-wide">
      <Select<Option, true> isMulti aria-label={label} placeholder="No channel" options={CHANNEL_OPTIONS} isDisabled={disabled}
        value={CHANNEL_OPTIONS.filter((o) => value.includes(o.value))} menuPosition="fixed"
        onChange={(list) => onChange(CHANNELS.filter((c) => (list ?? []).some((o) => o.value === c)))} />
    </div>
  );
}

const normal = (p: ChannelPreferences): ChannelPreferences => ({
  bySeverity: Object.fromEntries(SEVERITIES.map((s) => [s, p.bySeverity?.[s] ?? []])),
  workingHours: p.workingHours ?? null,
});

/* ---- my notification preferences ----------------------------------------------------------- */
export function MyPreferences({ compact = false }: { compact?: boolean }) {
  const h = useHerald();
  const saved = useV1(() => v1<ChannelPreferences>('/v1/preferences/me'), []);
  const [draft, setDraft] = useState<ChannelPreferences | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setDraft(null); }, [h.myRef]);
  if (saved.loading && !saved.data) return <Skeletons lines={4} height={32} />;
  if (saved.error && !saved.data) return <ErrorRetry error={saved.error} onRetry={saved.reload} />;
  const base = normal(saved.data ?? { bySeverity: {} });
  const p = draft ?? base;
  const dirty = JSON.stringify(p) !== JSON.stringify(base);
  const set = (next: ChannelPreferences) => setDraft(next);
  const tz = p.workingHours?.timezone ?? h.people[h.myRef.slice(7)]?.tz ?? 'Europe/Warsaw';
  const zones = (ZONES.includes(tz) ? ZONES : [tz, ...ZONES]).map((z) => ({ label: z, value: z }));
  const save = async () => {
    setBusy(true);
    const body = p.workingHours ? p : { bySeverity: p.bySeverity };
    const ok = await h.run(() => v1<ChannelPreferences>('/v1/preferences/me', { method: 'PUT', body }), 'Your preferences are saved. New alerts use them.');
    setBusy(false);
    if (ok) { setDraft(null); saved.reload(); }
  };
  return (
    <Stack space="space.200">
      {!compact && <Text color="color.text.subtle">How you ({h.me.name}) want to be told, by severity. A critical alert should reach you wherever you are; a warning can wait in email.</Text>}
      <div className="pref-grid">
        {SEVERITIES.map((s) => (
          <div key={s} style={{ display: 'contents' }}>
            <span><Severity value={s} /></span>
            <Channels label={`Channels for ${s}`} value={p.bySeverity[s] ?? []}
              onChange={(v) => set({ ...p, bySeverity: { ...p.bySeverity, [s]: v } })} />
          </div>
        ))}
      </div>
      <Inline space="space.150" alignBlock="end" shouldWrap>
        <Inline space="space.050" alignBlock="center">
          <Toggle id="wh-on" isChecked={!!p.workingHours} label="I have working hours"
            onChange={() => set({ ...p, workingHours: p.workingHours ? null : { from: '08:00', to: '18:00', timezone: tz } })} />
          <label htmlFor="wh-on"><Text>I have working hours</Text></label>
        </Inline>
        {p.workingHours && (
          <>
            <div style={{ width: 130 }}><Textfield type="time" aria-label="Working hours from" value={p.workingHours.from}
              onChange={(e) => { const from = e.currentTarget.value; set({ ...p, workingHours: { ...p.workingHours!, from } }); }} /></div>
            <Text>to</Text>
            <div style={{ width: 130 }}><Textfield type="time" aria-label="Working hours to" value={p.workingHours.to}
              onChange={(e) => { const to = e.currentTarget.value; set({ ...p, workingHours: { ...p.workingHours!, to } }); }} /></div>
            <div className="pick-wide"><Select<Option> aria-label="Time zone" options={zones} value={zones.find((z) => z.value === tz) ?? null} menuPosition="fixed"
              onChange={(o) => o && set({ ...p, workingHours: { ...p.workingHours!, timezone: o.value } })} /></div>
          </>
        )}
      </Inline>
      <Inline space="space.100" alignBlock="center">
        <Button appearance="primary" isDisabled={!dirty} isLoading={busy} onClick={save}>Save my preferences</Button>
        <Button appearance="subtle" isDisabled={!dirty} onClick={() => setDraft(null)}>Undo changes</Button>
        <Text size="small" color="color.text.subtle">{dirty ? 'Not saved yet.' : 'Saved.'}</Text>
      </Inline>
    </Stack>
  );
}

export function PreferencesPage() {
  return (
    <Page title="My notification preferences" lead="Choose the channels for each severity and when you are working. Rules that notify you follow these choices.">
      <div className="panel"><MyPreferences /></div>
    </Page>
  );
}

/* ---- team channel policy ------------------------------------------------------------------- */
function PolicyCard({ teamId, policy, canEdit, onSaved }: { teamId: string; policy: EffectivePolicy; canEdit: boolean; onSaved: () => void }) {
  const h = useHerald();
  const [draft, setDraft] = useState<EffectivePolicy | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [denied, setDenied] = useState<string | null>(null);
  const p = draft ?? policy;
  const dirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(policy);
  const own = policy.origin?.levels === 'team';
  const level = (i: number, patch: Partial<EscalationLevel>) =>
    setDraft({ ...p, levels: (p.levels ?? []).map((l, j) => (j === i ? { ...l, ...patch } : l)) });
  const failed = (e: unknown) => {
    if (e instanceof V1Error && e.status === 403) { setDenied('Only a Domain Root of this team’s domain or the Master can change the team’s channel policy.'); return; }
    h.notify({ appearance: 'error', title: 'Not saved', description: e instanceof Error ? e.message : String(e) });
  };
  const save = async () => {
    setBusy('save'); setDenied(null);
    try {
      await v1('/v1/escalation-policies', { method: 'PUT', body: {
        scopeType: 'team', scopeId: teamId, severity: p.severity,
        levels: (p.levels ?? []).map((l, i) => ({ level: i + 1, target: l.target, channels: l.channels, ackTimeoutSeconds: l.ackTimeoutSeconds ?? null, ...(l.target === 'custom' ? { recipients: l.recipients } : {}) })),
        renotifySeconds: p.renotifySeconds ?? null, softChannels: p.softChannels ?? [], softDigestMinutes: p.softDigestMinutes ?? null,
      } });
      h.notify({ appearance: 'success', title: `${p.severity.charAt(0).toUpperCase()}${p.severity.slice(1)} policy saved for this team` });
      setDraft(null); onSaved();
    } catch (e) { failed(e); }
    setBusy(null);
  };
  const reset = async () => {
    setBusy('reset'); setDenied(null);
    try {
      await v1(`/v1/escalation-policies?scopeType=team&scopeId=${encodeURIComponent(teamId)}&severity=${p.severity}`, { method: 'DELETE' });
      h.notify({ appearance: 'success', title: 'Back to the inherited policy' });
      setDraft(null); onSaved();
    } catch (e) { failed(e); }
    setBusy(null);
  };
  return (
    <div className="tile">
      <Stack space="space.150">
        <Inline spread="space-between" alignBlock="center">
          <Severity value={p.severity} />
          {policy.origin?.levels && <Lozenge appearance={own ? 'inprogress' : 'default'}>{ORIGIN[policy.origin.levels] ?? policy.origin.levels}</Lozenge>}
        </Inline>
        {(p.levels ?? []).map((l, i) => (
          <Stack key={i} space="space.050">
            <Text size="small" weight="semibold">Level {i + 1}: {TARGET[l.target] ?? l.target}{l.ackTimeoutSeconds ? ` · next level after ${Math.round(l.ackTimeoutSeconds / 60)} min` : ''}</Text>
            <Channels label={`Level ${i + 1} channels for ${p.severity}`} value={l.channels} disabled={!canEdit} onChange={(channels) => level(i, { channels })} />
          </Stack>
        ))}
        <Stack space="space.050">
          <Text size="small" weight="semibold">Kept informed{p.softDigestMinutes ? ` (summary every ${p.softDigestMinutes} min)` : ''}</Text>
          <Channels label={`Informed channels for ${p.severity}`} value={p.softChannels ?? []} disabled={!canEdit} onChange={(softChannels) => setDraft({ ...p, softChannels })} />
        </Stack>
        {denied && <SectionMessage appearance="warning"><Text>{denied}</Text></SectionMessage>}
        {canEdit && (
          <Inline space="space.100" shouldWrap>
            <Button appearance="primary" isDisabled={!dirty} isLoading={busy === 'save'} onClick={save}>Save for this team</Button>
            {dirty && <Button appearance="subtle" onClick={() => setDraft(null)}>Undo</Button>}
            {own && !dirty && <Button appearance="subtle" isLoading={busy === 'reset'} onClick={reset}>Reset to inherited</Button>}
          </Inline>
        )}
      </Stack>
    </div>
  );
}

export function TeamPolicy() {
  const h = useHerald();
  const teams = useV1(() => v1<V1Team[]>('/v1/teams'), []);
  const mine = h.teams[h.viewing.team]?.uuid;
  const [team, setTeam] = useState('');
  const list = useMemo(() => teams.data ?? [], [teams.data]);
  useEffect(() => {
    if (!list.length) return;
    if (!team || !list.some((t) => t.id === team)) setTeam(list.find((t) => t.id === mine)?.id ?? list[0].id);
  }, [list, mine, team]);
  const policies = useV1(
    () => (team ? Promise.all(SEVERITIES.map((s) => v1<EffectivePolicy>(`/v1/escalation-policies/effective?teamId=${encodeURIComponent(team)}&severity=${s}`))) : Promise.resolve([])),
    [team],
  );
  const options = list.map((t) => ({ label: t.name, value: t.id }));
  const canEdit = h.canApprove;   // a hint only: the service decides, and a refusal is shown on the card
  return (
    <Stack space="space.200">
      <Inline spread="space-between" alignBlock="center" shouldWrap space="space.150">
        <Heading size="small" as="h2">Team channel policy</Heading>
        <div className="pick-wide"><Select<Option> aria-label="Team" options={options} value={options.find((o) => o.value === team) ?? null}
          onChange={(o) => o && setTeam(o.value)} menuPosition="fixed" /></div>
      </Inline>
      <Text color="color.text.subtle">
        Which channels the team uses at each step of an escalation, per severity. {canEdit
          ? 'Changing a severity here overrides the inherited value for this team only.'
          : 'Domain Roots and the Master can change it; you can read it.'}
      </Text>
      {teams.error && !teams.data && <ErrorRetry error={teams.error} onRetry={teams.reload} />}
      {policies.error && !policies.data && <ErrorRetry error={policies.error} onRetry={policies.reload} />}
      {policies.loading && !policies.data ? <Skeletons lines={4} height={40} /> : (
        <div className="tiles">
          {(policies.data ?? []).map((p) => <PolicyCard key={`${team}-${p.severity}`} teamId={team} policy={p} canEdit={canEdit} onSaved={policies.reload} />)}
        </div>
      )}
    </Stack>
  );
}
