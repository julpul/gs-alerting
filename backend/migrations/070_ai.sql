-- P3-AI: knowledge docs carry the resolution they were built from (fixed-before / similar incidents).
ALTER TABLE knowledge_docs
    ADD COLUMN IF NOT EXISTS alert_id      uuid,
    ADD COLUMN IF NOT EXISTS resolution_id uuid,
    ADD COLUMN IF NOT EXISTS indexed_at    timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS knowledge_docs_resolution ON knowledge_docs(resolution_id)
    WHERE resolution_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS knowledge_docs_scope ON knowledge_docs(domain_id, team_id);
CREATE INDEX IF NOT EXISTS kc_scope ON knowledge_chunks(domain_id, team_id);
