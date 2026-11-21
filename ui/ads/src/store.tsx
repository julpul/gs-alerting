import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import { api, ApiError, identityHeaders, setIdentity, signInUrl, type Alert, type Directory, type Me, type Person, type Platform, type RuleRow, type Team } from './api';
import { resetIdentity } from './v1';

export type Viewing = { team: string; role: 'responder' | 'approver' | 'editor'; person?: string };
export type PersonaGroup = { name: string; items: { id: string; team: string | null; name: string; what: string }[] };
export type FlagItem = { id: number; title: string; description?: string; appearance: 'success' | 'error' | 'warning' | 'info' };
export type Scope = { id: string; name: string; owner: string; personal: boolean };

type Store = {
  me: Me;
  scopes: Scope[];                       // demo mode: the teams you can view as
  personas: PersonaGroup[];              // demo mode: everyone in the directory, by team
  viewTeams: Scope[];                    // demo mode: the teams the current person may look at
  viewing: Viewing; setViewing: (v: Viewing) => void;
  owner: string | null;                  // whose alerts are shown; null = everything (admin)
  myRef: string;                         // person:<id> of the current person
  myUserId: string | null;               // our user uuid of the current person (as /v1 names people)
  canEdit: boolean; canApprove: boolean;
  people: Record<string, Person>; teams: Record<string, Team>;
  alerts: Alert[]; platforms: Platform[]; rules: RuleRow[];
  tick: number;                          // goes up whenever something changed on the service
  ready: boolean; offline: boolean;
  refresh: () => Promise<void>;
  run: <T>(work: () => Promise<T>, done?: string | ((result: T) => string)) => Promise<T | undefined>;
  flags: FlagItem[]; notify: (flag: Omit<FlagItem, 'id'>) => void; dismissFlag: (id: number) => void;
  name: (ref: string | null | undefined) => string;
};

const Ctx = createContext<Store | null>(null);
export const useHerald = () => {
  const store = useContext(Ctx);
  if (!store) throw new Error('useHerald outside the provider');
  return store;
};

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const saved = (): Viewing | null => { try { return JSON.parse(localStorage.getItem('herald.ads.viewing') || 'null'); } catch { return null; } };

export function HeraldProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me>({ mode: 'demo', user: 'person:demo', name: 'Demo', role: 'admin', teams: [] });
  const [dir, setDir] = useState<Directory>({ teams: [], people: [] });
  const [viewing, setViewingState] = useState<Viewing>(saved() ?? { team: '', role: 'responder' });
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [rules, setRules] = useState<RuleRow[]>([]);
  const [tick, setTick] = useState(0);
  const [ready, setReady] = useState(false);
  const [offline, setOffline] = useState(false);
  const [flags, setFlags] = useState<FlagItem[]>([]);
  const flagId = useRef(0);
  const leaving = useRef(false);

  const people = useMemo(() => Object.fromEntries(dir.people.map((p) => [p.id, p])), [dir]);
  const teams = useMemo(() => Object.fromEntries(dir.teams.map((t) => [t.id, t])), [dir]);
  const scopes = useMemo<Scope[]>(() => [
    ...dir.teams.filter((t) => t.has_rules && t.id !== 'loadtest').map((t) => ({ id: t.id, name: t.name, owner: `team:${t.id}`, personal: false })),
    ...dir.people.filter((p) => p.has_rules).map((p) => ({ id: p.id, name: `${p.name} (personal)`, owner: `person:${p.id}`, personal: true })),
  ], [dir]);

  /* Everyone in the directory: grouped by team (lead, on-call person, members); people in no team with rules
     (Master, management, informed people) under "Organisation". */
  const personas = useMemo<PersonaGroup[]>(() => {
    const seen = new Set<string>();
    const named = (id: string) => people[id]?.name ?? id;
    const groups: PersonaGroup[] = dir.teams.filter((t) => t.has_rules && t.id !== 'loadtest').map((t) => ({
      name: t.name,
      items: [...new Set([t.lead, t.on_call, ...t.members].filter((x): x is string => !!x))].map((id) => {
        seen.add(id);
        return { id, team: t.id, name: named(id), what: id === t.lead ? 'Team lead, approves' : id === t.on_call ? 'On call now' : 'Member' };
      }),
    }));
    const elsewhere = (id: string) => dir.teams.find((t) => !(t.has_rules && t.id !== 'loadtest') && (t.members.includes(id) || t.lead === id))?.name;
    const org = dir.people.map((p) => p.id).filter((id) => !seen.has(id))
      .sort((a, b) => Number(b === 'master') - Number(a === 'master') || named(a).localeCompare(named(b)));
    return [
      { name: 'Organisation', items: org.map((id) => ({ id, team: null, name: named(id), what: id === 'master' ? 'Master: every team' : elsewhere(id) ? `Member of ${elsewhere(id)}` : 'Informed, not on a team' })) },
      ...groups,
    ].filter((g) => g.items.length);
  }, [dir, people]);

  /* The chosen person's role and teams, as the service sees them (GET /api/me as that person). */
  const [personInfo, setPersonInfo] = useState<{ id: string; role: string; teams: string[] } | null>(null);
  const chosen = viewing.person && people[viewing.person] ? viewing.person : null;
  const chosenInfo = personInfo && personInfo.id === chosen ? personInfo : null;

  /* Who am I, and whose alerts do I see. */
  const scope = (chosenInfo && !chosenInfo.teams.includes(viewing.team) ? scopes.find((s) => chosenInfo.teams.includes(s.id)) : undefined)
    ?? scopes.find((s) => s.id === viewing.team) ?? scopes[0];
  const demoPerson = useMemo(() => {
    if (chosen) return chosen;
    if (!scope) return '';
    if (scope.personal) return scope.id;
    const t = teams[scope.id];
    if (!t) return '';
    if (viewing.role === 'approver') return t.lead || t.on_call || t.members[0];
    if (viewing.role === 'editor') return t.members.find((m) => m !== t.on_call && m !== t.lead) || t.lead || t.on_call || t.members[0];
    return t.on_call || t.lead || t.members[0];
  }, [chosen, scope, teams, viewing.role]);
  useEffect(() => {
    if (!demoPerson || me.mode === 'oidc') { setPersonInfo(null); return; }
    let live = true;
    fetch('/api/me', { credentials: 'same-origin', headers: { Accept: 'application/json', 'X-Herald-User': `person:${demoPerson}` } })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: Me | null) => { if (live && d) setPersonInfo({ id: demoPerson, role: d.role, teams: d.teams ?? [] }); })
      .catch(() => { /* keep the guess */ });
    return () => { live = false; };
  }, [demoPerson, me.mode]);
  const info = personInfo && personInfo.id === demoPerson ? personInfo : null;
  const viewTeams = useMemo(() => scopes.filter((s) => !s.personal && (!info || info.teams.includes(s.id))), [scopes, info]);

  const sso = me.mode === 'oidc';
  const myRef = sso ? me.user : `person:${demoPerson}`;
  const personRole = chosen && info ? ({ admin: 'admin', approver: 'approver', editor: 'editor' } as Record<string, string>)[info.role] ?? 'responder' : null;
  const role = sso ? me.role : personRole ?? (chosen === 'master' ? 'admin' : viewing.role);
  const owner = sso ? (me.role === 'admin' ? null : me.teams[0] ? `team:${me.teams[0]}` : me.user) : chosen === 'master' && !viewing.team ? null : scope?.owner ?? null;
  // Set during render so every request made by this render's effects carries the right identity.
  setIdentity(me.mode, sso || !scope ? {} : {
    'X-Herald-User': myRef, 'X-Herald-Role': role, 'X-Herald-Teams': scope.personal ? '' : scope.id,
  });

  const notify = useCallback((flag: Omit<FlagItem, 'id'>) => {
    const id = ++flagId.current;
    setFlags((all) => [{ ...flag, id }, ...all].slice(0, 4));
    window.setTimeout(() => setFlags((all) => all.filter((f) => f.id !== id)), 7000);
  }, []);
  const dismissFlag = useCallback((id: number) => setFlags((all) => all.filter((f) => f.id !== id)), []);

  const failed = useCallback((error: unknown) => {
    if (error instanceof ApiError && error.status === 401 && sso && !leaving.current) {
      leaving.current = true;
      const stepUp = error.detail === 'step_up_required';
      notify({ appearance: 'info', title: stepUp ? 'Confirm it is you' : 'Your session has ended', description: 'Taking you to sign in…' });
      window.setTimeout(() => location.assign(signInUrl(stepUp, me.provider)), 900);
      return;
    }
    notify({ appearance: 'error', title: 'That did not work', description: error instanceof Error ? error.message : String(error) });
  }, [me.provider, notify, sso]);

  const refresh = useCallback(async () => {
    if (!ready) return;
    try {
      const q = owner ? `&owner=${encodeURIComponent(owner)}` : '';
      const [a, p] = await Promise.all([api<Alert[]>(`/api/alerts?limit=200${q}`), api<Platform[]>('/api/platforms')]);
      setAlerts(a); setPlatforms(p); setOffline(false); setTick((t) => t + 1);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) failed(error); else setOffline(true);
    }
  }, [failed, owner, ready]);

  const run = useCallback(async <T,>(work: () => Promise<T>, done?: string | ((result: T) => string)) => {
    try {
      const result = await work();
      if (done) notify({ appearance: 'success', title: typeof done === 'function' ? done(result) : done });
      await refresh();
      return result;
    } catch (error) {
      failed(error);
      await refresh();
      return undefined;
    }
  }, [failed, notify, refresh]);

  /* Start: who is signed in, then the directory. */
  useEffect(() => {
    (async () => {
      let who: Me | null = null;
      try {
        const r = await fetch('/api/me', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
        if (r.status === 401) { location.assign(signInUrl(false)); return; }
        const d = await r.json();
        if (r.ok && d?.mode === 'oidc') who = d;
      } catch { /* demo mode */ }
      if (who) { setMe(who); setIdentity('oidc', {}); }
      try {
        setDir(await api<Directory>('/api/directory'));
        setReady(true);
      } catch { setOffline(true); }
    })();
  }, []);

  /* Load, and reload when the person or team changes. */
  useEffect(() => {
    if (!ready) return;
    void refresh();
    api<RuleRow[]>('/api/rules').then(setRules).catch(() => setRules([]));
  }, [ready, owner, myRef, role, refresh]);

  /* The /v1 screens hold a token for one person: drop it when the person changes. */
  useEffect(() => { if (ready) resetIdentity(); }, [ready, myRef]);

  /* Live updates: the service pushes a line whenever an alert changes; a slow poll covers a dropped connection. */
  useEffect(() => {
    if (!ready) return;
    const ctl = new AbortController();
    let timer = 0;
    const soon = () => { window.clearTimeout(timer); timer = window.setTimeout(() => void refresh(), 150); };
    (async () => {
      while (!ctl.signal.aborted) {
        try {
          const r = await fetch(`/api/stream${owner ? `?owner=${encodeURIComponent(owner)}` : ''}`, {
            headers: { Accept: 'text/event-stream', ...identityHeaders() }, signal: ctl.signal, credentials: 'same-origin',
          });
          if (!r.ok || !r.body) throw new Error('no stream');
          const reader = r.body.getReader();
          const decoder = new TextDecoder();
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            if (decoder.decode(value, { stream: true }).includes('data:')) soon();
          }
        } catch { /* reconnect below */ }
        await new Promise((resolve) => window.setTimeout(resolve, 3000));
      }
    })();
    const poll = window.setInterval(() => void refresh(), 15000);
    return () => { ctl.abort(); window.clearInterval(poll); window.clearTimeout(timer); };
  }, [ready, owner, myRef, refresh]);

  const setViewing = useCallback((v: Viewing) => {
    setViewingState(v);
    try { localStorage.setItem('herald.ads.viewing', JSON.stringify(v)); } catch { /* private window */ }
  }, []);

  const name = useCallback((ref: string | null | undefined) => {
    const r = String(ref ?? '');
    if (!r) return '';
    if (r.startsWith('system:')) return 'Herald (automatic)';
    if (r.startsWith('person:')) return people[r.slice(7)]?.name ?? cap(r.slice(7));
    if (r.startsWith('team:')) {
      const id = r.slice(5).replace('/on-call', '');
      const n = teams[id]?.name ?? cap(id);
      return r.endsWith('/on-call') ? `${n} on-call person` : `${n} team`;
    }
    return r;
  }, [people, teams]);

  const value: Store = {
    me: sso ? me : { ...me, user: myRef, name: people[demoPerson]?.name ?? 'Demo', role },
    scopes, personas, viewTeams, viewing: { team: owner === null && !sso ? '' : scope?.id ?? '', role: viewing.role, person: chosen ?? demoPerson }, setViewing, owner, myRef,
    myUserId: sso ? me.userId ?? null : people[demoPerson]?.uuid ?? null,
    canEdit: ['editor', 'approver', 'admin'].includes(role), canApprove: ['approver', 'admin'].includes(role),
    people, teams, alerts, platforms, rules, tick, ready, offline, refresh, run, flags, notify, dismissFlag, name,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/* Load something for one screen, and load it again when the service says something changed. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]): { data: T | undefined; error: string | null; loading: boolean } {
  const { tick, ready } = useHerald();
  const [state, setState] = useState<{ data: T | undefined; error: string | null; loading: boolean }>({ data: undefined, error: null, loading: true });
  useEffect(() => {
    if (!ready) return;
    let live = true;
    load().then(
      (data) => live && setState({ data, error: null, loading: false }),
      (error) => live && setState((s) => ({ data: s.data, error: error instanceof Error ? error.message : String(error), loading: false })),
    );
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ready, ...deps]);
  return state;
}
