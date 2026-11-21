/* Pieces shared by the /v1 screens: the AI label and draft frame, live loading, countdowns, errors with retry,
   skeletons and the "show raw" toggle. Every AI output goes through AiDraft, so it is always labelled and never
   applied without a click. */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

import Button from '@atlaskit/button/new';
import { CodeBlock } from '@atlaskit/code';
import AiIcon from '@atlaskit/icon/core/ai-sparkle';
import Lozenge from '@atlaskit/lozenge';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import SectionMessage, { SectionMessageAction } from '@atlaskit/section-message';
import Skeleton from '@atlaskit/skeleton';
import Toggle from '@atlaskit/toggle';

import { useHerald } from './store';
import { RECONNECTED, stream, type StreamEvent } from './v1';

/* ---- AI labelling ----------------------------------------------------------------------- */
export function AiLabel({ text = 'AI suggestion' }: { text?: string }) {
  return (
    <span className="ai-label">
      <AiIcon label="" size="small" />
      <Lozenge appearance="new">{text}</Lozenge>
    </span>
  );
}

export function AiDraft({ title, label, children, actions, footnote }: {
  title: string; label?: string; children: ReactNode; actions?: ReactNode; footnote?: string;
}) {
  return (
    <section className="ai-draft" aria-label={`${label ?? 'AI suggestion'}: ${title}`}>
      <Stack space="space.150">
        <Inline space="space.100" alignBlock="center" shouldWrap>
          <AiLabel text={label} />
          <Text weight="semibold">{title}</Text>
        </Inline>
        {children}
        {actions && <Inline space="space.100" shouldWrap>{actions}</Inline>}
        <Text size="small" color="color.text.subtlest">{footnote ?? 'Written by AI. Check it before you use it; nothing is applied until you press a button.'}</Text>
      </Stack>
    </section>
  );
}

/* ---- live data -------------------------------------------------------------------------- */
export function useStream(types: readonly string[], handler: (event: StreamEvent) => void) {
  const ref = useRef(handler);
  ref.current = handler;
  const key = types.join(',');
  useEffect(() => stream.subscribe((e) => {
    if (e.type === RECONNECTED || key.split(',').includes(e.type)) ref.current(e);
  }), [key]);
}

export type Loaded<T> = { data: T | undefined; error: string | null; loading: boolean; reload: () => void };

/* Load for one screen; load again when one of `on` arrives on the live stream (or it reconnects), when the
   person changes, or every `every` ms. A failed reload keeps the last data and shows the error beside it. */
export function useV1<T>(load: () => Promise<T>, deps: unknown[], opts: { on?: readonly string[]; every?: number } = {}): Loaded<T> {
  const { ready, myRef } = useHerald();
  const [state, setState] = useState<{ data: T | undefined; error: string | null; loading: boolean }>({ data: undefined, error: null, loading: true });
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  useEffect(() => {
    if (!ready) return;
    let live = true;
    load().then(
      (data) => live && setState({ data, error: null, loading: false }),
      (error) => live && setState((s) => ({ data: s.data, error: error instanceof Error ? error.message : String(error), loading: false })),
    );
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, myRef, nonce, ...deps]);
  useStream(opts.on ?? [], reload);
  useEffect(() => {
    if (!opts.every) return;
    const t = window.setInterval(reload, opts.every);
    return () => window.clearInterval(t);
  }, [opts.every, reload]);
  return { ...state, reload };
}

/* ---- time ------------------------------------------------------------------------------- */
export function useNow(stepMs = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), stepMs);
    return () => window.clearInterval(t);
  }, [stepMs]);
  return now;
}
export function mmss(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${rest}` : `${m}:${rest}`;
}
export function Countdown({ until, prefix, past }: { until: string | null | undefined; prefix: string; past: string }) {
  const now = useNow();
  if (!until) return null;
  const left = (Date.parse(until) - now) / 1000;
  return <span className="countdown">{left > 0 ? `${prefix} ${mmss(left)}` : past}</span>;
}

/* ---- states ----------------------------------------------------------------------------- */
export function ErrorRetry({ error, onRetry, title = 'This did not load' }: { error: string; onRetry: () => void; title?: string }) {
  return (
    <SectionMessage appearance="error" title={title} actions={[<SectionMessageAction key="retry" onClick={onRetry}>Try again</SectionMessageAction>]}>
      <Text>{error}</Text>
    </SectionMessage>
  );
}
export function Skeletons({ lines = 3, height = 16 }: { lines?: number; height?: number }) {
  return (
    <Stack space="space.100" testId="skeleton">
      {Array.from({ length: lines }, (_, i) => <Skeleton key={i} width={`${100 - i * 15}%`} height={height} isShimmering />)}
    </Stack>
  );
}
export function RawToggle({ value, label = 'Show raw data', id }: { value: unknown; label?: string; id: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Stack space="space.100">
      <Inline space="space.050" alignBlock="center">
        <Toggle id={id} isChecked={open} onChange={() => setOpen((v) => !v)} label={label} />
        <label htmlFor={id}><Text size="small" color="color.text.subtle">{label}</Text></label>
      </Inline>
      {open && <CodeBlock language="json" showLineNumbers={false} text={JSON.stringify(value, null, 2)} />}
    </Stack>
  );
}
export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setDone(true); window.setTimeout(() => setDone(false), 1500); } catch { /* clipboard blocked */ }
  };
  return <Button onClick={copy}>{done ? 'Copied' : label}</Button>;
}

/* ---- risk ------------------------------------------------------------------------------- */
export function Risk({ value }: { value: string | undefined | null }) {
  if (value === 'high') return <Lozenge appearance="removed" isBold>High risk</Lozenge>;
  if (value === 'medium') return <Lozenge appearance="moved">Medium risk</Lozenge>;
  return <Lozenge appearance="success">Low risk</Lozenge>;
}
export const percent = (x: number | undefined) => (x === undefined ? '' : `${Math.round(x * 100)}%`);
