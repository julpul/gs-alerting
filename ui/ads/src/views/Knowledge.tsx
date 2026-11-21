/* Knowledge: what the AI may quote when it suggests a fix. Resolution notes land here by themselves; people
   add Markdown, text or a link (GET/POST /v1/knowledge). */
import { useState } from 'react';

import Button from '@atlaskit/button/new';
import EmptyState from '@atlaskit/empty-state';
import Heading from '@atlaskit/heading';
import Lozenge from '@atlaskit/lozenge';
import { Inline, Stack, Text } from '@atlaskit/primitives/compiled';
import Select from '@atlaskit/select';
import Tabs, { Tab, TabList, TabPanel } from '@atlaskit/tabs';
import TextArea from '@atlaskit/textarea';
import Textfield from '@atlaskit/textfield';

import { ErrorRetry, Skeletons, useV1 } from '../ai';
import { useHerald } from '../store';
import { Table } from '../Table';
import { ago, docTitle, Page } from '../ui';
import { v1, type KnowledgeDoc, type V1Team } from '../v1';

type Option = { label: string; value: string };
const KIND: Record<string, string> = { upload: 'Document', link: 'Web page', resolution_note: 'How it was fixed', jira: 'Jira', confluence: 'Confluence' };
const MAX_BYTES = 1024 * 1024;

function AddDocument({ teams, onAdded }: { teams: V1Team[]; onAdded: () => void }) {
  const h = useHerald();
  const [mode, setMode] = useState(0);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [url, setUrl] = useState('');
  const [team, setTeam] = useState(teams[0]?.id ?? '');
  const [busy, setBusy] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const options: Option[] = teams.map((t) => ({ label: t.name, value: t.id }));
  const ready = mode === 1 ? /^https?:\/\/\S+$/.test(url.trim()) : content.trim().length > 0;

  const pickFile = async (file: File | undefined) => {
    setFileError(null);
    if (!file) return;
    if (file.size > MAX_BYTES) { setFileError('That file is larger than 1 MB.'); return; }
    const text = await file.text();
    setContent(text);
    if (!title) setTitle(docTitle(text, false) ?? file.name.replace(/\.(md|markdown|txt)$/i, ''));
  };
  const add = async () => {
    setBusy(true);
    const named = title.trim() || (mode === 0 ? docTitle(content, false) ?? '' : '');
    const body = { ...(named ? { title: named } : {}), ...(team ? { teamId: team } : {}), ...(mode === 1 ? { url: url.trim() } : { content }) };
    const ok = await h.run(() => v1<KnowledgeDoc>('/v1/knowledge', { body }), (d) => `“${d.title}” added. The AI can use it from now on.`);
    setBusy(false);
    if (ok) { setTitle(''); setContent(''); setUrl(''); onAdded(); }
  };
  return (
    <div className="panel">
      <Stack space="space.150">
        <Heading size="xsmall" as="h2">Add to knowledge</Heading>
        <Tabs id="knowledge-mode" selected={mode} onChange={setMode}>
          <TabList><Tab>Text or Markdown</Tab><Tab>Link</Tab></TabList>
          <TabPanel>
            <div className="tab-body">
              <Stack space="space.100">
                <TextArea aria-label="Document text" minimumRows={6} value={content} onChange={(e) => setContent(e.currentTarget.value)} placeholder="Paste a page, a post-mortem or notes. Markdown works." />
                <Inline space="space.100" alignBlock="center">
                  <label className="file-pick">
                    <input type="file" accept=".md,.markdown,.txt,text/plain,text/markdown" onChange={(e) => void pickFile(e.currentTarget.files?.[0])} />
                    <span>…or choose a .md / .txt file</span>
                  </label>
                  {fileError && <Text color="color.text.danger">{fileError}</Text>}
                </Inline>
              </Stack>
            </div>
          </TabPanel>
          <TabPanel>
            <div className="tab-body">
              <Textfield aria-label="Address of the page" placeholder="https://wiki.example.com/payments/runbook" value={url} onChange={(e) => setUrl(e.currentTarget.value)} />
            </div>
          </TabPanel>
        </Tabs>
        <Inline space="space.100" alignBlock="center" shouldWrap>
          <div style={{ width: 280 }}><Textfield aria-label="Title" placeholder="Title (optional)" value={title} onChange={(e) => setTitle(e.currentTarget.value)} /></div>
          <div className="pick-wide"><Select<Option> aria-label="Team" placeholder="Team" options={options} value={options.find((o) => o.value === team) ?? null} onChange={(o) => setTeam(o?.value ?? '')} menuPosition="fixed" /></div>
          <Button appearance="primary" isLoading={busy} isDisabled={!ready} onClick={add}>Add</Button>
        </Inline>
      </Stack>
    </div>
  );
}

export function Knowledge() {
  const docs = useV1(() => v1<KnowledgeDoc[]>('/v1/knowledge'), []);
  const teams = useV1(() => v1<V1Team[]>('/v1/teams'), []);
  const teamName = (id: string | null | undefined) => teams.data?.find((t) => t.id === id)?.name ?? (id ? '' : 'Whole domain');
  return (
    <Page title="Knowledge" lead="What the AI may quote when it suggests a fix. Every resolved alert's note is added here by itself.">
      <Stack space="space.300">
        {teams.data && <AddDocument teams={teams.data} onAdded={docs.reload} />}
        {docs.loading && !docs.data ? <Skeletons lines={5} height={24} /> : docs.error && !docs.data ? <ErrorRetry error={docs.error} onRetry={docs.reload} /> : (
          <Table
            head={{ cells: [{ key: 'title', content: 'Title', isSortable: true }, { key: 'kind', content: 'Kind', width: 16 }, { key: 'team', content: 'Team', width: 18 }, { key: 'at', content: 'Indexed', width: 14 }] }}
            rows={(docs.data ?? []).map((d) => ({ key: d.id, cells: [
              { key: d.title, content: <Text weight="medium">{d.title}</Text> },
              { key: d.kind, content: <Lozenge appearance={d.kind === 'resolution_note' ? 'success' : 'default'}>{KIND[d.kind] ?? d.kind}</Lozenge> },
              { key: 'team', content: teamName(d.teamId) },
              { key: 'at', content: d.indexedAt ? ago(d.indexedAt) : 'Waiting' },
            ] }))}
            rowsPerPage={25}
            emptyView={<EmptyState header="Nothing here yet" description="Add a runbook page or a post-mortem above. Resolving an alert with a note also adds one." />}
          />
        )}
      </Stack>
    </Page>
  );
}
