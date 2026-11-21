/* On-call & escalation: who is on call now and next for each team (GET /v1/teams/{id}/oncall), and the
   escalation ladder that applies after inheritance, with where each value comes from. */
import { useEffect, useState } from 'react';

import Heading from '@atlaskit/heading';
import Lozenge from '@atlaskit/lozenge';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import Select from '@atlaskit/select';
import Tabs, { Tab, TabList, TabPanel } from '@atlaskit/tabs';

import { ErrorRetry, Skeletons, useV1 } from '../ai';
import { channelName, clock, Page, Severity, Who } from '../ui';
import { actorName, v1, type EffectivePolicy, type OnCall as OnCallNow, type V1Team } from '../v1';

type Option = { label: string; value: string };
const SEVERITIES = ['critical', 'error', 'warning', 'info'] as const;
const ORIGIN: Record<string, string> = { org: 'from Organisation', domain: 'from Domain', team: 'set for this team', rule: 'from the rule' };
const TARGET: Record<string, string> = { oncall: 'Whoever is on call', domain_root: 'Domain Root', master: 'Master', custom: 'Chosen people' };

const minutes = (s: number | null | undefined) => (s ? (s % 60 === 0 ? `${s / 60} min` : `${s} s`) : '');
const until = (iso: string) => clock(iso);

function Origin({ of, policy }: { of: string; policy: EffectivePolicy }) {
  const o = policy.origin?.[of];
  if (!o) return null;
  return <Lozenge appearance={o === 'team' ? 'new' : 'default'}>{ORIGIN[o] ?? `from ${o}`}</Lozenge>;
}

function TeamCard({ team }: { team: V1Team }) {
  const oc = useV1(() => v1<OnCallNow>(`/v1/teams/${team.id}/oncall`), [team.id], { every: 60000 });
  return (
    <div className="tile">
      <Heading size="xsmall" as="h3">{team.name}</Heading>
      {oc.loading && !oc.data ? <Skeletons lines={2} /> : oc.error && !oc.data ? <ErrorRetry error={oc.error} onRetry={oc.reload} /> : (
        <Stack space="space.100">
          <Stack space="space.050">
            <Text size="small" weight="semibold" color="color.text.subtle">On call now</Text>
            {oc.data?.now ? <Who who={`person:${oc.data.now.userName ?? oc.data.now.userId}`} /> : <Text color="color.text.warning">Nobody. Alerts go straight to the next level.</Text>}
            {oc.data?.now && <Text size="small" color="color.text.subtlest">until {until(oc.data.now.end)}</Text>}
          </Stack>
          <Stack space="space.050">
            <Text size="small" weight="semibold" color="color.text.subtle">Next</Text>
            {oc.data?.next ? <Text>{oc.data.next.userName ?? oc.data.next.userId} · from {until(oc.data.next.start)}</Text> : <Text color="color.text.subtlest">No next shift planned</Text>}
          </Stack>
          {!!oc.data?.escalationChain?.length && (
            <Text size="small" color="color.text.subtle">If nobody answers: {oc.data.escalationChain.map((a, i) => `${i + 1}. ${actorName(a)}`).join(' → ')}</Text>
          )}
        </Stack>
      )}
    </div>
  );
}

function Ladder({ teamId, severity }: { teamId: string; severity: string }) {
  const p = useV1(() => v1<EffectivePolicy>(`/v1/escalation-policies/effective?teamId=${encodeURIComponent(teamId)}&severity=${severity}`), [teamId, severity]);
  if (p.loading && !p.data) return <Skeletons lines={4} height={24} />;
  if (p.error && !p.data) return <ErrorRetry error={p.error} onRetry={p.reload} />;
  const policy = p.data;
  if (!policy) return null;
  const levels = policy.levels ?? [];
  return (
    <Stack space="space.200">
      <Inline space="space.100" alignBlock="center"><Severity value={severity} /><Text weight="semibold">Who is told, and when</Text><Origin of="levels" policy={policy} /></Inline>
      {levels.length ? (
        <ol className="ladder">
          {levels.map((l) => (
            <li key={l.level}>
              <Text weight="semibold">Level {l.level}: {TARGET[l.target] ?? l.target}</Text>
              <Text color="color.text.subtle"> by {l.channels.length ? l.channels.map(channelName).join(', ').toLowerCase() : 'Herald only'}</Text>
              {l.ackTimeoutSeconds ? <div className="ladder-wait"><Text size="small" color="color.text.subtle">↓ after {minutes(l.ackTimeoutSeconds)} without acknowledgement</Text></div> : null}
            </li>
          ))}
        </ol>
      ) : <Text color="color.text.subtle">Nobody is paged for {severity} alerts; they show in Herald only.</Text>}
      <Inline space="space.100" alignBlock="center" shouldWrap>
        <Text>Repeat: {policy.renotifySeconds ? `every ${minutes(policy.renotifySeconds)} until someone acknowledges` : 'no'}</Text><Origin of="renotifySeconds" policy={policy} />
      </Inline>
      <Inline space="space.100" alignBlock="center" shouldWrap>
        <Text>Informed people: {policy.softChannels?.length ? policy.softChannels.map(channelName).join(', ').toLowerCase() : 'not told'}{policy.softDigestMinutes ? `, in a digest every ${policy.softDigestMinutes} min` : policy.softChannels?.length ? ', straight away' : ''}</Text>
        <Origin of="softChannels" policy={policy} />
      </Inline>
      <Text size="small" color="color.text.subtlest">To change these, a Domain Root or Master opens Settings → Escalation in the classic interface.</Text>
    </Stack>
  );
}

export function OnCall() {
  const teams = useV1(() => v1<V1Team[]>('/v1/teams'), []);
  const [team, setTeam] = useState('');
  const [sev, setSev] = useState(0);
  useEffect(() => { if (!team && teams.data?.length) setTeam(teams.data[0].id); }, [team, teams.data]);
  const options: Option[] = (teams.data ?? []).map((t) => ({ label: t.name, value: t.id }));
  return (
    <Page title="On-call & escalation" lead="Who answers first, and who hears about it if they don't.">
      {teams.loading && !teams.data ? <Skeletons lines={3} height={64} /> : teams.error && !teams.data ? <ErrorRetry error={teams.error} onRetry={teams.reload} /> : (
        <Stack space="space.400">
          {(teams.data ?? []).length ? <div className="tiles">{(teams.data ?? []).map((t) => <TeamCard key={t.id} team={t} />)}</div>
            : <Text color="color.text.subtle">You are not in any team yet. Ask your Domain Root to add you.</Text>}
          {team && (
            <Stack space="space.200">
              <Inline space="space.150" alignBlock="center" shouldWrap>
                <Heading size="small" as="h2">Escalation for</Heading>
                <div className="pick-wide"><Select<Option> aria-label="Team" options={options} value={options.find((o) => o.value === team) ?? null} onChange={(o) => o && setTeam(o.value)} menuPosition="fixed" /></div>
              </Inline>
              <Tabs id="severity-tabs" selected={sev} onChange={setSev}>
                <TabList>{SEVERITIES.map((s) => <Tab key={s}>{s.charAt(0).toUpperCase() + s.slice(1)}</Tab>)}</TabList>
                {SEVERITIES.map((s, i) => <TabPanel key={s}><div className="tab-body">{sev === i && <Ladder teamId={team} severity={s} />}</div></TabPanel>)}
              </Tabs>
            </Stack>
          )}
        </Stack>
      )}
    </Page>
  );
}
