/* Talking to the public /v1 API (docs/openapi.yaml) for the AI, agents, approvals, on-call and health screens.

   Who you are: the service hands the page a short-lived /v1 token for the current identity (the "Viewing as"
   choice in demo mode, the session under single sign-on) at GET /api/session-token. Before that endpoint
   exists (a service without the Herald adapters), demo mode falls back to signing in as the demo person.
   A 401 drops the token and tries once more with a fresh one. */
import { identityHeaders } from './api';

/* ---- contract types (only the fields these screens read) ---------------------------------- */
export type Actor = { kind: 'user' | 'agent' | 'system'; id: string; displayName: string; onBehalfOf?: string | null };
export type Severity = 'critical' | 'error' | 'warning' | 'info';
export type V1Step = {
  id: string; order?: number; kind?: 'text' | 'check' | 'action'; text?: string; action?: string | null;
  parameters?: Record<string, unknown>; approval?: 'none' | 'required'; risk?: 'low' | 'medium' | 'high';
  state?: string; suggested?: boolean; url?: string | null; parsedBy?: string | null;
};
export type V1Runbook = {
  id?: string; name?: string; format?: string; source?: string; steps?: V1Step[]; teamId?: string | null;
  attached?: Record<string, unknown>; parsedBy?: string | null;
};
export type V1Alert = {
  id: string; shortId?: string; title: string; severity: Severity; status: 'open' | 'acknowledged' | 'escalated' | 'resolved';
  count: number; teamId?: string; teamName?: string; escalationLevel?: number; escalatesAt?: string | null;
  ackDeadline?: string | null; flags?: string[]; runbook?: V1Runbook | null;
};
export type Suggestion = {
  id: string; alertId: string; status: 'draft' | 'accepted' | 'rejected'; diagnosis?: string; nextStep?: string;
  nextStepId?: string | null; reusedFrom?: string; aiUnavailable?: boolean; confidence?: number;
  fixedBefore?: { alertId: string; resolvedAt?: string; resolutionNote?: string }[];
  similarIncidents?: { alertId: string; title?: string; similarity?: number; resolutionNote?: string }[];
  sources?: { kind: string; title?: string; url?: string }[];
  draftPullRequestUrl?: string | null;
};
export type NoteDraft = { note: string; aiUnavailable?: boolean; model?: string | null };
export type Approval = {
  id: string; shortId?: number | null; status: 'pending' | 'approved' | 'rejected' | 'expired';
  origin?: 'runbook_step' | 'agent_request'; requestedBy?: Actor; alertId?: string | null; action: string;
  parameters?: Record<string, unknown>; risk?: 'low' | 'medium' | 'high'; reason?: string; approvers?: Actor[];
  decidedBy?: Actor | null; comment?: string | null; expiresAt?: string; createdAt?: string;
};
export type PersonalToken = { id: string; name: string; prefix: string; createdAt: string; lastUsedAt?: string | null };
export type PersonalTokenCreated = PersonalToken & { token: string };
export type Recipient = { kind: 'user' | 'team' | 'oncall' | 'role'; id: string };
export type Condition = { field?: string; operator?: string; value?: unknown; op?: 'and' | 'or'; conditions?: Condition[] };
export type Rule = {
  id?: string; version?: number; name: string; teamId: string; enabled?: boolean; priority?: Severity;
  match: Condition; routing: { hard?: Recipient[]; soft?: Recipient[]; channelsBySeverity?: Record<string, string[]>; timeWindow?: Record<string, unknown> };
  presentation?: { titleTemplate?: string; bodyTemplate?: string; fields?: string[]; color?: string };
  [key: string]: unknown;
};
export type DryRun = {
  evaluated?: number; matched?: number; alertsAfterDedup?: number;
  notifications?: { recipient: Actor; kind: 'hard' | 'soft'; count: number }[];
};
export type RuleDraft = { draft: Rule; explanation?: string; dryRun?: DryRun };
export type Source = { id: string; name: string; teamId?: string; type?: string; preset?: string | null; eventsLast24h?: number; lastEventAt?: string | null; mappingStatus?: string };
export type RawEvent = { eventId: string; receivedAt: string; payload: Record<string, unknown> };
export type Mapping = { status?: 'draft' | 'accepted'; detectedSchema?: string; fields?: Record<string, string>; severityValueMap?: Record<string, string>; explanation?: string };
export type V1Team = { id: string; name: string; domainIds?: string[]; memberIds?: string[] };
export type V1User = { id: string; email?: string; displayName?: string };
export type Shift = { id?: string; userId: string; userName?: string | null; start: string; end: string; kind?: string };
export type OnCall = { teamId: string; now: Shift | null; next: Shift | null; escalationChain?: Actor[] };
export type EscalationLevel = { level: number; target: string; recipients?: Recipient[]; channels: string[]; ackTimeoutSeconds?: number | null };
export type EffectivePolicy = {
  scopeType: string; scopeId: string; severity: Severity; levels?: EscalationLevel[]; renotifySeconds?: number | null;
  softChannels?: string[]; softDigestMinutes?: number | null; origin?: Record<string, string>;
};
export type KnowledgeDoc = { id: string; title: string; kind: string; teamId?: string | null; indexedAt?: string | null };
export type Latency = { p50: number | null; p99: number | null; count: number };
export type MetricsSummary = {
  eventsPerSecond?: number; latencyP50Ms?: number | null; latencyP99Ms?: number | null; queueDepth?: number | null;
  eventsIngested?: number; notificationsDelivered?: number; inDeadLetter?: number; lost?: number | null; windowMinutes?: number;
  outcomes?: Record<string, number>; queues?: Record<string, number> | null; processingLatencyMs?: Latency | null;
  deliveryLatencyMs?: Record<string, Latency>;
};
export type StreamEvent = { type: string; at: string; payload: Record<string, unknown> };

/* ---- errors ------------------------------------------------------------------------------- */
export class V1Error extends Error {
  constructor(message: string, public status: number, public code?: string) { super(message); }
  get aiUnavailable() { return this.code === 'ai_unavailable' || this.status === 503; }
}

/* ---- the token ---------------------------------------------------------------------------- */
type Token = { value: string; expires: number; who: string };
let cached: Token | null = null;
let pending: Promise<Token> | null = null;

const who = () => identityHeaders()['X-Herald-User'] ?? 'session';

async function fetchToken(): Promise<Token> {
  const identity = who();
  const r = await fetch('/api/session-token', { credentials: 'same-origin', headers: { Accept: 'application/json', ...identityHeaders() } });
  let body: { accessToken?: string; expiresIn?: number } | null = null;
  if (r.ok) body = await r.json();
  else if (r.status === 404 && identity.startsWith('person:')) {
    // Demo fallback while the Herald adapters are not deployed: the seeded demo people all use the password "demo".
    const login = await fetch('/v1/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ email: `${identity.slice(7)}@demo`, password: 'demo' }),
    });
    if (login.ok) body = await login.json();
  }
  if (!body?.accessToken) throw new V1Error('Could not get permission to talk to the service. Reload the page.', r.status || 401, 'no_token');
  return { value: body.accessToken, expires: Date.now() + Math.max(30, (body.expiresIn ?? 900) - 30) * 1000, who: identity };
}

export async function token(fresh = false): Promise<string> {
  if (!fresh && cached && cached.who === who() && cached.expires > Date.now()) return cached.value;
  if (!pending) pending = fetchToken().finally(() => { pending = null; });
  cached = await pending;
  return cached.value;
}

/* Called when the person changes: the next request and the live stream use the new identity. */
export function resetIdentity() {
  cached = null;
  stream.restart();
}

/* ---- requests ----------------------------------------------------------------------------- */
async function errorOf(r: Response): Promise<V1Error> {
  let data: { code?: string; message?: string; detail?: unknown } | null = null;
  try { data = await r.json(); } catch { /* no body */ }
  const message = data?.message ?? (typeof data?.detail === 'string' ? data.detail : `The service answered ${r.status}`);
  return new V1Error(message, r.status, data?.code);
}

export async function v1<T>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const send = async (fresh: boolean) => {
    const h: Record<string, string> = { Accept: 'application/json', Authorization: `Bearer ${await token(fresh)}` };
    if (opts.body !== undefined) h['Content-Type'] = 'application/json';
    return fetch(path, {
      method: opts.method ?? (opts.body !== undefined ? 'POST' : 'GET'),
      headers: h, body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  };
  let r = await send(false);
  if (r.status === 401) r = await send(true);
  if (!r.ok) throw await errorOf(r);
  if (r.status === 204) return undefined as T;
  const text = await r.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/* Our alert ids in the Herald screens are the short numbers; /v1 takes them as "A-<n>". */
export const alertRef = (id: number | string) => (typeof id === 'number' || /^\d+$/.test(id) ? `A-${id}` : id);
export const alertNumber = (ref: string | null | undefined): number | null => {
  const m = /^A-(\d+)$/i.exec(ref ?? '');
  return m ? Number(m[1]) : null;
};
export const actorName = (a: Actor | null | undefined) =>
  !a ? 'Someone' : a.kind === 'agent' ? `agent ${a.displayName}${a.onBehalfOf ? ` on behalf of ${a.onBehalfOf}` : ''}` : a.kind === 'system' ? 'Herald (automatic)' : a.displayName;

/* ---- Server-Sent Events: one shared EventSource for every screen ---------------------------- */
type Listener = (event: StreamEvent) => void;
export const STREAM_TYPES = [
  'alert.created', 'alert.updated', 'alert.acked', 'alert.escalated', 'alert.resolved',
  'delivery.status', 'suggestion.ready', 'approval.requested', 'approval.decided',
] as const;
export const RECONNECTED = 'stream.reconnected';

class Stream {
  private listeners = new Set<Listener>();
  private source: EventSource | null = null;
  private timer = 0;
  private generation = 0;
  live = false;

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    if (!this.source && !this.timer) void this.open(false);
    return () => {
      this.listeners.delete(fn);
      if (!this.listeners.size) this.close();
    };
  }

  restart() {
    if (!this.listeners.size) return;
    this.close();
    void this.open(true);
  }

  private close() {
    this.generation += 1;
    window.clearTimeout(this.timer);
    this.timer = 0;
    this.source?.close();
    this.source = null;
    this.live = false;
  }

  private emit(event: StreamEvent) { this.listeners.forEach((fn) => fn(event)); }

  private async open(fresh: boolean) {
    const generation = ++this.generation;
    let access: string;
    try { access = await token(fresh); } catch { this.retry(generation); return; }
    if (generation !== this.generation) return;
    const source = new EventSource(`/v1/stream?access_token=${encodeURIComponent(access)}`);
    this.source = source;
    source.onopen = () => {
      this.live = true;
      this.emit({ type: RECONNECTED, at: new Date().toISOString(), payload: {} });  // events may have been missed
    };
    const relay = (e: MessageEvent) => {
      try { this.emit(JSON.parse(e.data) as StreamEvent); } catch { /* not ours */ }
    };
    STREAM_TYPES.forEach((t) => source.addEventListener(t, relay as EventListener));
    source.onerror = () => {
      this.live = false;
      if (source.readyState === EventSource.CLOSED) { source.close(); this.retry(generation); }
    };
  }

  private retry(generation: number) {
    if (generation !== this.generation || !this.listeners.size) return;
    this.source = null;
    this.timer = window.setTimeout(() => { this.timer = 0; void this.open(true); }, 3000);
  }
}
export const stream = new Stream();

/* ---- the copilot: POST + a streamed answer (EventSource cannot POST) ------------------------- */
export type ChatEvent =
  | { event: 'token'; data: { text: string } }
  | { event: 'tool_call'; data: { name: string; args: Record<string, unknown> } }
  | { event: 'tool_result'; data: { name: string; summary: string } }
  | { event: 'draft'; data: { kind: 'rule' | 'mapping' | 'runbook' | 'approval'; id: string | null; data: Record<string, unknown> } }
  | { event: 'done'; data: Record<string, never> }
  | { event: 'error'; data: { code: string; message: string } };

/* Splits a text/event-stream body into frames; returns the unfinished tail. */
export function parseFrames(buffer: string, onFrame: (event: string, data: string) => void): string {
  const parts = buffer.replace(/\r\n/g, '\n').split('\n\n');
  const tail = parts.pop() ?? '';
  for (const part of parts) {
    let event = 'message';
    const data: string[] = [];
    for (const line of part.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    }
    if (data.length) onFrame(event, data.join('\n'));
  }
  return tail;
}

export async function chat(
  messages: { role: 'user' | 'assistant'; content: string }[],
  context: Record<string, unknown>,
  onEvent: (e: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const send = async (fresh: boolean) => fetch('/v1/agent/chat', {
    method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', Authorization: `Bearer ${await token(fresh)}` },
    body: JSON.stringify({ messages, context }),
  });
  let r = await send(false);
  if (r.status === 401) r = await send(true);
  if (!r.ok || !r.body) throw await errorOf(r);
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const frame = (event: string, data: string) => {
    try { onEvent({ event, data: JSON.parse(data) } as ChatEvent); } catch { /* malformed frame */ }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer = parseFrames(buffer + decoder.decode(value, { stream: true }), frame);
  }
  parseFrames(`${buffer}\n\n`, frame);
}
