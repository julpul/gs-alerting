-- Complete schema of TECH_SPEC 4.6 (+ slugs, user timezone). Org scope uses the fixed org uuid
-- (app/core/ids.py ORG_ID) in scope_id columns, so scope_id is NOT NULL everywhere.

CREATE EXTENSION IF NOT EXISTS vector;

-- identity & org
CREATE TABLE users (
    id            uuid PRIMARY KEY,
    email         text NOT NULL UNIQUE,
    display_name  text NOT NULL,
    password_hash text NOT NULL,
    phone         text,
    manager_id    uuid REFERENCES users(id) ON DELETE SET NULL,
    timezone      text NOT NULL DEFAULT 'Europe/Warsaw',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE domains (
    id   uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    name text NOT NULL UNIQUE
);

CREATE TABLE teams (
    id   uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    name text NOT NULL
);

CREATE TABLE team_domains (
    team_id   uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    PRIMARY KEY (team_id, domain_id)
);
CREATE INDEX team_domains_domain ON team_domains(domain_id);

CREATE TABLE team_members (
    team_id uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (team_id, user_id)
);
CREATE INDEX team_members_user ON team_members(user_id);

CREATE TABLE role_assignments (
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       text NOT NULL CHECK (role IN ('master', 'domain_root', 'member', 'informed')),
    scope_type text NOT NULL CHECK (scope_type IN ('org', 'domain', 'team')),
    scope_id   uuid NOT NULL,
    granted_by uuid,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role, scope_type, scope_id)
);

CREATE TABLE personal_tokens (
    id           uuid PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   text NOT NULL UNIQUE,
    name         text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);

-- sources & rules
CREATE TABLE sources (
    id              uuid PRIMARY KEY,
    name            text NOT NULL,
    team_id         uuid NOT NULL,
    domain_id       uuid NOT NULL,
    type            text NOT NULL,
    preset          text,
    key_prefix      text NOT NULL,
    key_hash        text NOT NULL UNIQUE,
    config          jsonb NOT NULL DEFAULT '{}',
    mapping         jsonb,
    mapping_status  text,
    default_severity text NOT NULL DEFAULT 'warning',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sources_team ON sources(team_id);

CREATE TABLE rules (
    id         uuid PRIMARY KEY,
    team_id    uuid NOT NULL,
    name       text NOT NULL,
    enabled    boolean NOT NULL DEFAULT true,
    version    int NOT NULL DEFAULT 1,
    definition jsonb NOT NULL,
    created_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rules_team ON rules(team_id);

CREATE TABLE rule_versions (
    rule_id    uuid NOT NULL,
    version    int NOT NULL,
    definition jsonb NOT NULL,
    changed_by uuid,
    changed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_id, version)
);

-- alerts & delivery
CREATE TABLE alerts (
    id                      uuid PRIMARY KEY,
    short_id                bigserial UNIQUE,
    team_id                 uuid NOT NULL,
    domain_id               uuid NOT NULL,
    rule_id                 uuid,
    source_id               uuid,
    fingerprint             text NOT NULL,
    knowledge_key           text,
    title                   text NOT NULL,
    body                    text,
    severity                text NOT NULL,
    color                   text,
    status                  text NOT NULL DEFAULT 'open',
    count                   int NOT NULL DEFAULT 1,
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_seen_at            timestamptz NOT NULL DEFAULT now(),
    first_event_received_at timestamptz,
    escalation_level        int NOT NULL DEFAULT 1,
    ack_deadline            timestamptz,
    next_renotify_at        timestamptz,
    acked_at                timestamptz,
    acked_by                uuid,
    resolved_at             timestamptz,
    assignee_id             uuid,
    flags                   text[] NOT NULL DEFAULT '{}',
    fields                  jsonb NOT NULL DEFAULT '{}',
    raw_event               jsonb,
    grouped_items           jsonb NOT NULL DEFAULT '[]',
    runbook_id              uuid,
    created_at              timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX alerts_open_fp ON alerts(rule_id, fingerprint) WHERE status <> 'resolved';
CREATE INDEX alerts_team_status ON alerts(team_id, status, last_seen_at DESC);
CREATE INDEX alerts_deadline ON alerts(ack_deadline) WHERE status IN ('open', 'escalated');
CREATE INDEX alerts_renotify ON alerts(next_renotify_at) WHERE next_renotify_at IS NOT NULL;
CREATE INDEX alerts_knowledge_key ON alerts(domain_id, knowledge_key);

CREATE TABLE deliveries (
    id                  uuid PRIMARY KEY,
    alert_id            uuid NOT NULL,
    recipient_user_id   uuid,
    recipient_address   text,
    channel             text NOT NULL,
    kind                text NOT NULL CHECK (kind IN ('hard', 'soft')),
    notification_seq    int NOT NULL DEFAULT 1,
    idempotency_key     text NOT NULL UNIQUE,
    status              text NOT NULL DEFAULT 'pending',
    attempts            int NOT NULL DEFAULT 0,
    last_error          text,
    priority            int NOT NULL DEFAULT 0,
    event_received_at   timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    sent_at             timestamptz,
    delivered_at        timestamptz
);
CREATE INDEX deliveries_alert ON deliveries(alert_id);
CREATE INDEX deliveries_status ON deliveries(status, created_at);

CREATE TABLE outbox (
    id           bigserial PRIMARY KEY,
    topic        text NOT NULL,
    routing_key  text NOT NULL,
    priority     int NOT NULL DEFAULT 0,
    payload      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
CREATE INDEX outbox_unpublished ON outbox(created_at) WHERE published_at IS NULL;

CREATE TABLE timeline (
    id       uuid PRIMARY KEY,
    alert_id uuid NOT NULL,
    at       timestamptz NOT NULL DEFAULT now(),
    type     text NOT NULL,
    actor    jsonb NOT NULL DEFAULT '{}',
    text     text NOT NULL,
    data     jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX timeline_alert ON timeline(alert_id, at);

-- escalation & on-call
CREATE TABLE escalation_policies (
    scope_type         text NOT NULL CHECK (scope_type IN ('org', 'domain', 'team', 'rule')),
    scope_id           uuid NOT NULL,
    severity           text NOT NULL,
    levels             jsonb NOT NULL DEFAULT '[]',
    renotify_seconds   int,
    soft_channels      text[] NOT NULL DEFAULT '{}',
    soft_digest_minutes int,
    updated_by         uuid,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id, severity)
);

CREATE TABLE rotations (
    id             uuid PRIMARY KEY,
    team_id        uuid NOT NULL,
    name           text NOT NULL,
    member_ids     uuid[] NOT NULL DEFAULT '{}',
    starts_at      timestamptz NOT NULL,
    shift_hours    int NOT NULL,
    handover_time  text,
    timezone       text NOT NULL DEFAULT 'Europe/Warsaw'
);

CREATE TABLE shifts (
    id          uuid PRIMARY KEY,
    team_id     uuid NOT NULL,
    user_id     uuid NOT NULL,
    start_at    timestamptz NOT NULL,
    end_at      timestamptz NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('rotation', 'override', 'manual')),
    rotation_id uuid,
    created_by  uuid
);
CREATE INDEX shifts_team_time ON shifts(team_id, start_at, end_at);

CREATE TABLE preferences (
    user_id       uuid PRIMARY KEY,
    by_severity   jsonb NOT NULL DEFAULT '{}',
    working_hours jsonb NOT NULL DEFAULT '{}'
);

-- runbooks, actions, approvals
CREATE TABLE runbooks (
    id       uuid PRIMARY KEY,
    name     text NOT NULL,
    format   text NOT NULL,
    source   text,
    steps    jsonb NOT NULL DEFAULT '[]',
    attached jsonb NOT NULL DEFAULT '{}',
    team_id  uuid
);

CREATE TABLE step_runs (
    id          uuid PRIMARY KEY,
    alert_id    uuid NOT NULL,
    step_id     text NOT NULL,
    state       text NOT NULL,
    approval_id uuid,
    output      text,
    run_by      jsonb NOT NULL DEFAULT '{}',
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX step_runs_alert ON step_runs(alert_id, at);

CREATE TABLE approvals (
    id                uuid PRIMARY KEY,
    status            text NOT NULL DEFAULT 'pending',
    origin            text NOT NULL CHECK (origin IN ('runbook_step', 'agent_request')),
    requested_by      jsonb NOT NULL DEFAULT '{}',
    alert_id          uuid,
    action            text NOT NULL,
    parameters        jsonb NOT NULL DEFAULT '{}',
    risk              text,
    reason            text,
    approver_user_ids uuid[] NOT NULL DEFAULT '{}',
    decided_by        jsonb,
    comment           text,
    expires_at        timestamptz NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX approvals_status ON approvals(status, expires_at);

CREATE TABLE action_trust (
    domain_id  uuid NOT NULL,
    action     text NOT NULL,
    level      text NOT NULL,
    cap        text,
    successes  int NOT NULL DEFAULT 0,
    failures   int NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (domain_id, action)
);

-- AI & knowledge
CREATE TABLE suggestions (
    id         uuid PRIMARY KEY,
    alert_id   uuid NOT NULL,
    status     text NOT NULL,
    content    jsonb NOT NULL DEFAULT '{}',
    model      text,
    latency_ms int,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX suggestions_alert ON suggestions(alert_id, created_at DESC);

CREATE TABLE resolutions (
    id            uuid PRIMARY KEY,
    alert_id      uuid NOT NULL,
    domain_id     uuid NOT NULL,
    team_id       uuid NOT NULL,
    knowledge_key text,
    note          text NOT NULL,
    resolved_by   uuid,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX resolutions_key ON resolutions(domain_id, knowledge_key);

CREATE TABLE knowledge_docs (
    id         uuid PRIMARY KEY,
    domain_id  uuid NOT NULL,
    team_id    uuid,
    kind       text NOT NULL,
    title      text NOT NULL,
    url        text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_chunks (
    id        uuid PRIMARY KEY,
    doc_id    uuid NOT NULL,
    domain_id uuid NOT NULL,
    team_id   uuid,
    text      text NOT NULL,
    embedding vector(384),
    tsv       tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED
);
CREATE INDEX kc_hnsw ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX kc_tsv ON knowledge_chunks USING gin (tsv);
CREATE INDEX kc_doc ON knowledge_chunks(doc_id);

-- integrations & audit
CREATE TABLE integrations (
    domain_id        uuid NOT NULL,
    kind             text NOT NULL,
    config_encrypted bytea,
    mode             text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (domain_id, kind)
);

CREATE TABLE webhook_subscriptions (
    id               uuid PRIMARY KEY,
    team_id          uuid NOT NULL,
    url              text NOT NULL,
    secret_encrypted bytea,
    event_types      text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor       jsonb NOT NULL,
    action      text NOT NULL,
    target_type text NOT NULL,
    target_id   text NOT NULL,
    data        jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_target ON audit_log(target_type, target_id, at DESC);
