# gs-alerting — Flexible Alerting & Notification Service

Goldman Sachs Hackathon 2026 · Software Engineering track.

**One place where people *and* AI agents raise problems, the problem reaches the right person on the right channel, and they fix it together with the team's runbooks — while AI never acts without permissions and evidence.**

Three differentiators:

1. **Pyramid for humans and agents.** Permissions flow down (Master → Domain Root → Member), escalation flows up. Agents raise alerts and ask for approvals through the same routing (MCP `raise_alert`, `request_approval`).
2. **Safe hands.** No AI on the hot path; AI output is always a draft; poisoned-telemetry defenses; risky actions always need a human; actions earn autonomy on a trust ladder.
3. **Team memory.** Every resolution becomes knowledge. The next similar alert shows "fixed before" instantly (exact key lookup, no model) plus similar incidents (vector + full-text search).

---

## Quick start

```bash
cp .env.example .env     # optional: add an LLM key (OpenAI / Anthropic / OpenRouter) for AI features
make up                  # full stack in Docker: api, workers, postgres, redis, rabbitmq, mailpit, mockgw, simulator, grafana; migrates + seeds
make demo-events         # sends the brief's Appendix A events plus a prompt-injection sample
```

Needs Docker and [uv](https://docs.astral.sh/uv/) (for `make demo-events`). Fresh clone to first alert: the three commands above (first build downloads the local embedding model).

| What | URL | Notes |
| --- | --- | --- |
| API + Swagger | http://localhost:8080/docs | contract in [`docs/openapi.yaml`](docs/openapi.yaml); `/readyz`, `/metrics` |
| Mailpit (email inbox) | http://localhost:8025 | every email alert lands here |
| mockgw (SMS / phone gateway) | http://localhost:8090 | phone-like log page, reply box, "Press 1 to ACK" |
| Simulator "Vistula Capital" | http://localhost:8095 | control panel: traffic rate, trigger incidents |
| RabbitMQ management | http://localhost:15672 | guest / guest |
| Grafana | http://localhost:3001 | provisioned dashboard (anonymous admin) |
| Prometheus | http://localhost:9090 | |

Demo logins (password `demo` for everyone; full org in [`docs/DEMO_ORG.md`](docs/DEMO_ORG.md)):
`master@demo` (Master) · `root.payments@demo`, `root.settlements@demo`, `root.security@demo`, `root.markets@demo` (Domain Roots) · `anna@demo` (Payments on-call) · `piotr@demo` (Settlements on-call) · `manager@demo` (Informed, soft notifications only).

```bash
TOKEN=$(curl -s localhost:8080/v1/auth/login -H 'content-type: application/json' \
        -d '{"email":"master@demo","password":"demo"}' | jq -r .accessToken)
curl -s localhost:8080/v1/alerts -H "authorization: Bearer $TOKEN" | jq '.items[0]'
curl -s localhost:8080/v1/stream?access_token=$TOKEN        # live SSE feed
```

Note: the web UI is built by the design team in a separate codebase against `docs/openapi.yaml` (see [`docs/HANDOFF_UI.md`](docs/HANDOFF_UI.md)). This repo is the complete backend: REST + SSE + MCP, plus Swagger, Mailpit and the mockgw page to see every channel working.

## Live demo on Railway

| Service | URL |
| --- | --- |
| API / Swagger (`/docs`) | https://api-production-d033bedd.up.railway.app |
| Mailpit inbox | https://mailpit-production-69850428.up.railway.app |
| mockgw (SMS / phone) | https://mockgw-production.up.railway.app |
| Simulator | https://simulator-production-ae5fd2bb.up.railway.app |

Same code, same seed (the full Vistula Capital org), same logins. Postgres, Redis and RabbitMQ are private services. Layout, variables and the CD flow: [`infra/RAILWAY.md`](infra/RAILWAY.md).

---

## Architecture

```
                 ┌──────────────────────────────────────────────┐
  Sources ──────►│ API (N replicas)  ingest · REST · SSE · MCP  │◄──── UI (SSE)
  (webhooks,     └──────┬──────────────────────────▲────────────┘◄──── Agents (MCP)
   RabbitMQ via         │ publish (confirm) → 202  │ SSE fan-out (Redis pub/sub)
   connector,           ▼                          │
   agents)        ┌────────────┐                   │
                  │ RabbitMQ   │ events.in (priority)
                  └─────┬──────┘                   │
                        ▼                          │
                  ┌──────────────────────┐   ┌─────┴─────┐
                  │ Worker (N)           │──►│  Redis    │ rule reload, recent events,
                  │ map · rules · dedup  │   └───────────┘ idempotency, rate limits, pub/sub
                  │ group · route        │──►┌───────────┐
                  └─────┬────────────┬───┘   │ Postgres  │ alerts, deliveries, outbox,
                        │            │       │ +pgvector │ rules, runbooks, knowledge, audit
          deliveries.*  ▼            ▼ ai.tasks└─────▲─────┘
                  ┌────────────┐  ┌──────────────┐   │
                  │ Delivery(N)│  │ AI worker    │───┘  LLM (OpenAI / Anthropic / OpenRouter via LiteLLM)
                  │ adapters   │  │ (async, off  │────► fastembed (local embeddings)
                  └─────┬──────┘  │  hot path)   │
                        ▼         └──────────────┘
         Mailpit · mockgw (SMS/phone) · SSE · webhooks
                  ▲
       ┌──────────┴───────────┐
       │ Scheduler (1-N)      │ ack deadlines → escalation, renotify, outbox relay,
       └──────────────────────┘ digests, shift generation, approval expiry
```

One image, one codebase, several roles (`python -m app.main --role <role>`; a comma list or `all` runs several in one process, which is what Railway uses):

| Role | Responsibility | Scale |
| --- | --- | --- |
| `api` | ingest, REST, SSE, MCP (`/mcp`), auth | N replicas |
| `worker` | consume `events.in`: normalize, map, match rules, dedup/group; alert + deliveries + outbox in one transaction | N (main scaling knob) |
| `delivery` | consume `deliveries.*`: send through channel adapters, retries, DLQ, fallback channel | N |
| `scheduler` | 1 s loop: escalation, renotify, outbox relay, shift generation, digests, expiry; `FOR UPDATE SKIP LOCKED` | 1–N |
| `ai` | consume `ai.tasks`: fix suggestions, embeddings, resolution notes | 1–N |
| `connector` | pull customer RabbitMQ queues configured as sources into `events.in` | 1 per source group |

**Why no per-event Postgres writes.** Writing every event to Postgres would make the database the throughput ceiling. Only alerts (after dedup), deliveries and outbox rows are persisted. Raw events live in RabbitMQ until processed and in a bounded Redis stream (`recent:{source}`, ~5000) for sample events, dry-run and the "unrouted" view. 500 identical errors are one upsert target (one alert, `count` 500), not 500 inserts. Event lifecycle, queues, Redis keys, tables and failure modes: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How it maps to the brief

| Brief | What we built | Where in code |
| --- | --- | --- |
| **2.1** Ingest from many sources, any schema | Webhook (`POST /v1/ingest/{key}`, batch up to 1000), RabbitMQ connector, typed `POST /v1/alerts/raise`, MCP. Schema detection + per-source mapping (presets Alertmanager / Sentry / Datadog / CloudEvents, heuristic, AI-suggested). Unknown schemas still alert on heuristics | `app/api/routers/ingest.py`, `app/connectors/`, `app/pipeline/{schema_detect,mapping,mapping_heuristic,normalize}.py`, `app/pipeline/presets/` |
| **2.2** Rules on any field, AND/OR, routing, presentation, at runtime | One rule model for YAML / API / AI: nested all/any/not, operators (`>`, `in`, `regex`, `exists`, ...), per-rule routing (hard/soft, time windows, channels by severity), title/body/field/colour templates, dedup/grouping/throttle. Hot reload over Redis pub/sub, dry-run against recent events, rule from plain text | `app/pipeline/rules/`, `app/api/routers/rules.py`, `app/ai/rule_copilot.py` |
| **2.3** Preferred channel, preferences, failure survival | Email (SMTP/Mailpit), SMS, phone (mockgw), UI (SSE), webhook (HMAC). Per-user preferences + working hours, rotations, escalation policies with inheritance. Retries 5 s / 30 s / 120 s, DLQ after 3 failures, fallback `phone → sms → email` (the UI delivery is always created), 3 idempotency layers, transactional outbox | `app/delivery/`, `app/escalation/`, `app/core/{outbox,outbox_relay,rabbit}.py` |
| **2.4** Service UI backend | Live feed per team (SSE), ack / resolve / assign / comment, rules / sources / preferences / on-call management, AI suggestion accept/reject, per-rule presentation. 52-path contract; UI in a separate codebase | `app/api/routers/`, `app/core/sse.py`, `docs/openapi.yaml` |
| **2.5** Don't flood people | Fingerprint dedup (partial unique index + upsert), time-window grouping, notify on state change only, per-recipient throttle into a digest email, auto-resolve from Alertmanager `resolved` | `app/pipeline/{fingerprint,alert_writer}.py`, `app/delivery/routing.py`, `app/escalation/jobs.py` |
| **2.6** Runbooks, actions, approvals, audit | 6 input formats (YAML, JSON, Markdown, checklist, free text, link) parsed to one step model; `{{template}}` filling; action registry; approvals via UI, SMS reply or MCP; step runs + audit; trust ladder; resolutions feed knowledge | `app/runbooks/`, `app/escalation/inbound.py`, `app/knowledge/`, `app/ai/resolution_note.py` |
| **Scalability** | Stateless roles, queue-absorbed spikes, priorities, measured scaling (below) | `app/main.py`, `app/core/rabbit.py`, `loadtest/` |
| **Throughput / latency** | k6 scripts T1–T6, chaos test with reconciliation, report generator; results below | `loadtest/` |
| **Innovation** | Agent approvals via MCP, poisoned-telemetry defenses, trust ladder, team memory, storm reuse guard | `app/mcp/`, `app/pipeline/injection.py`, `app/runbooks/trust.py`, `app/ai/` |

## Flexibility: how to add ...

| To add | Effort | How / where |
| --- | --- | --- |
| **A source** | config only, no code | `POST /v1/sources` (webhook or `type: rabbitmq` + `config.queue`) with an optional `preset`; the API returns the key and ready-made `curl` / Python snippets (`GET /v1/sources/{id}/snippets`). Seeded examples: `backend/seed/fixtures/sources.yaml` |
| **A schema** | config only | `POST /v1/sources/{id}/mapping/suggest` (AI proposes a mapping from sample events; falls back to a deterministic heuristic when no model is available), edit, `PUT /v1/sources/{id}/mapping`. Presets for Alertmanager, Sentry, Datadog, CloudEvents: `app/pipeline/presets/mappings.py` |
| **A rule** | config only | API (`POST /v1/rules`), YAML (`GET/PUT /v1/rules/{id}/yaml`), or plain text (`POST /v1/rules/from-text` returns a draft + dry-run). Active in all workers through Redis pub/sub `rules:changed` (spec target: < 2 s, no restart). Seed examples: `backend/seed/fixtures/rules.yaml` |
| **A channel** | one class | `app/delivery/channels/<name>.py` with a class decorated `@register`; a Twilio SMS adapter is sketched in [`app/delivery/channels/README.md`](backend/app/delivery/channels/README.md) |
| **An action** | one file | `app/runbooks/actions/<name>.py`: class + `@register` + pydantic params + default risk; runbooks then use it by name. See [`app/runbooks/actions/README.md`](backend/app/runbooks/actions/README.md) |
| **A runbook** | content only | `POST /v1/runbooks/parse` turns pasted YAML / Markdown / text / link into steps (draft), `POST /v1/runbooks` saves and attaches to rules/schemas/alert types |
| **A runbook format** | one parser module | `app/runbooks/parsers/` (`yaml`, `markdown`, `text`, `link` today); `detect_format` routes unseen formats to the heuristics so a draft is always produced |
| **A protocol** | one class | `app/connectors/`: implement `async def run(publish)` and register in `CONNECTORS`; see [`app/connectors/README.md`](backend/app/connectors/README.md) |

## Scalability

- **Stateless roles.** All state lives in Postgres (durable), Redis (ephemeral), RabbitMQ (in flight). Scale with `make scale N=4` (`docker compose --scale worker=N --scale delivery=N`) or Railway replicas. The rule index is cached per worker and reloaded by pub/sub, so there is no shared hot state.
- **The queue absorbs spikes.** Ingest only persists to RabbitMQ (publisher confirm) and answers `202`; processing may lag and catch up. If the broker is down the API answers `503 Retry-After`: we never accept what we cannot persist.
- **Priorities.** `events.in` and `deliveries.*` are priority queues (critical 9, error 6, warning 3, info 0): under overload critical alerts are consumed first, info waits but is not dropped.
- **Coordination without leaders.** Scheduler jobs use `FOR UPDATE SKIP LOCKED`, so any number of scheduler replicas is safe.
- **Idempotency in three layers.** Event level: Redis `SET evt:{id} NX`. Delivery level: unique `idempotency_key` in Postgres. Provider level: `Idempotency-Key` header (honoured by mockgw). Delivery is at-least-once, effectively once.
- **Little work per event.** No per-event Postgres write, dedup collapses duplicates into one row update, compiled and indexed rules, orjson, uvloop, asyncpg pool, prefetch 200, batch ingest, cached source keys, one Redis round trip per event.
- **Honest SPOFs.** The demo runs a single RabbitMQ, Postgres and Redis. Production notes: RabbitMQ quorum queues on 3 nodes, Postgres primary + replica, Redis Sentinel, OIDC instead of JWT demo logins. KEDA / Azure Container Apps autoscaling on queue length is a design, **not built** (no `infra/k8s` manifests in this repo).

## Measured performance

Measured, not estimated. Conditions: one MacBook with 10 cores, **everything including k6 on the same host in Docker** (so numbers are conservative), real RabbitMQ / Postgres / Redis. Raw results and charts: [`loadtest/results/`](loadtest/results/).

| Test | Result | Target |
| --- | --- | --- |
| T1 latency ingest → UI p50 / p99 (100 ev/s, 5 min, 6,404 alerts) | **5.6 ms / 52 ms** | < 50 ms / < 300 ms |
| T1 latency ingest → email / SMS / phone p99 | **73 / 78 / 78 ms** | < 1 s |
| T1 pipeline processing p50 / p99 (every event, Prometheus) | 3.2 ms / 24.8 ms | — |
| T2 max sustained throughput, 4 workers (p99 processing < 1 s) | **1,500–2,500 ev/s** (host-bound; best run 2,500 on a clean DB) | ≥ 1,000 ev/s |
| T4 chaos: SMS gateway down 60 s + worker `SIGKILL` at 200 ev/s | **0 lost** (6,454 / 6,454 alerts reconciled, all deliveries accounted) | 0 |
| T5 scaling 1 / 2 / 4 workers | 1,250 / 1,750 / 1,500 ev/s — one worker process ≈ 1,250 ev/s; beyond 2 workers the shared 10-core laptop (k6 + Postgres + RabbitMQ + AI on the same host) is the limit, not the service | roughly linear on separate hosts |
| T6 dedup, 500 identical events in 60 s | **1 alert ×500**, 1 phone + 1 SMS + 1 UI notification | 1 alert, 1 hard notification |

Worst-case load profile: 20 % of events match a rule and every matching event carries a unique id, so each one creates a *new* alert with deliveries (a realistic storm deduplicates far more). Perf work that got us here: uvloop for every role, one Redis round trip per event, Postgres group commit (`commit_delay`), duplicate fast path (plain UPDATE instead of upsert), AI worker CPU cap + storm reuse of suggestions (off the hot path).

Reproduce (stack up with `make up`; needs `brew install k6 uv`):

```bash
make t1 t2 t4 t5 t6 report     # k6 + chaos + reconciliation; writes loadtest/results/<date>/report.md + PNGs
```

Details, knobs and how `LOST` is computed: [`loadtest/README.md`](loadtest/README.md). `T4` needs at least two workers (`make scale N=2`). Latency definitions: delivery latency = `deliveries.delivered_at − event_received_at` (SQL percentiles); processing latency = worker commit − `received_at` (Prometheus histogram).

## Noise control

| Mechanism | Behaviour |
| --- | --- |
| **Deduplication** | `fingerprint = sha1(rule + dedup fields)` (default: rendered title); partial unique index on open alerts + upsert → one alert, `count++`, `last_seen_at`. A resolved alert that recurs opens a new one |
| **Grouping** | `rule.grouping`: fingerprint also contains the group key and a time bucket; each event appends to `groupedItems` (cap 100) → e.g. one alert per instrument per 5 minutes |
| **State-change notifications** | deliveries are created only when an alert is created, escalated, re-opened, raised in severity or resolved; counter increments only update the feed via SSE |
| **Throttle + digest** | per recipient and channel per minute (`maxPerRecipientPerMinute`, default 10); overflow is `suppressed_digest` and later sent as one digest email by the scheduler (`digested`). Counted, never lost |
| **Auto-resolve** | Alertmanager `status: resolved` resolves the matching open alert (outcome `auto_resolved`) |

## Runbooks & approvals

- **Formats in** (all become one step model): structured YAML/JSON (Appendix B), Markdown pages and checklists, free text, wiki / Confluence links (link stored, short link in SMS). An unseen format falls back to heuristics, and with an LLM configured, `POST /v1/runbooks/parse` drafts steps from it. Steps are filled with event values (`{{workflowId}}`) and rendered per channel (full steps in UI and email, short link in SMS).
- **Actions** (registry): `http_get`, `retry_workflow`, `restart_service`, `failover_region`, `rollback_deploy`, `open_ticket`, `webhook` (HMAC signed), `escalate`, `notify_text`. Workflow / service / ticket actions call mockgw.
- **Approvals.** A step needs approval when it is marked `approval: required`, when its risk is `high`, or when its trust level is below `auto`. Approve from the UI, **by SMS reply** (`APPROVE <id>` / `REJECT <id>` to mockgw → `/v1/inbound/sms`), or let an agent wait on it through MCP `request_approval` (long-poll).
- **Trust ladder** per (domain, action): `suggest → run_after_approval → auto`. Five consecutive approved successes promote one level (never above a cap set by the Domain Root; high-risk actions never reach `auto`); any failure demotes one level.
- **Audit.** Step runs, approvals, ack/resolve, AI accept/reject land in the alert timeline and `audit_log` with actor (user, agent on behalf of user, system). Resolving an alert stores a resolution note that feeds team memory.
- **Inbound SMS shared secret.** `POST /v1/inbound/sms` has no bearer token. Set `INBOUND_SMS_SECRET` (default empty = open, for local dev) on the backend and on mockgw: the gateway then sends `X-Inbound-Secret`, and the backend compares it in constant time and answers `401` otherwise. Set it for any deployment that is reachable from the internet.

## AI

- **Off the hot path.** Ingest → alert → notification never calls a model. The `ai` role works from `ai.tasks` after the alert is committed and notified; target < 5 s and not part of the delivery latency.
- **Drafts only.** Suggestions, mappings, rules and runbooks from AI are drafts with Accept / Reject; accepts are audited. The model can only point at runbook steps that already exist; it cannot invent executable commands.
- **Poisoned-telemetry defenses.** (1) Event content reaches the model only inside `<untrusted_event>` blocks after an instruction that it is data. (2) Constrained output (existing step ids, validated citations). (3) A deterministic detector scores every string field (instruction phrases, command patterns, base64 blobs); a hit sets the `suspicious_content` flag, shows a banner and disables AI action suggestions for that alert. (4) High-risk actions always need a human. `make demo-events` includes an injection sample.
- **Redaction** before any model call: AWS keys, GitHub tokens, JWTs, private keys, `password=` / `token=` params, emails, phones, IBAN, card numbers.
- **Providers.** LiteLLM router: OpenAI, Anthropic, OpenRouter; two slots (`LLM_MODEL_FAST`, `LLM_MODEL_SMART`) plus a `LLM_FALLBACKS` chain, 20 s timeout, 1 retry, one repair round for invalid JSON. Per-domain own key or endpoint is stored Fernet-encrypted and never returned. Unit tests use a stub; the product always uses a real model.
- **Storm reuse guard.** Sibling alerts (same rule and knowledge key within 120 s) reuse the first suggestion instead of making N model calls; the suggestion carries `reusedFrom`.
- **Degraded mode.** All providers down or no key: everything except AI drafts keeps working; suggestions show the deterministic part (fixed-before, matching runbook, similar incidents) labelled `aiUnavailable`.
- **Memory.** L1 exact `knowledge_key` lookup over past resolutions (index, no model); L2 hybrid retrieval: local fastembed embeddings (`BAAI/bge-small-en-v1.5`) + pgvector + Postgres full-text, reciprocal rank fusion. GitHub / Datadog context in suggestions is **not built**.

## Agents (MCP)

`/mcp` (streamable HTTP, same `api` process) authenticated with a personal access token; every call runs with the token owner's scope and is audited as "agent X on behalf of Y".

```bash
# create a PAT (shown once)
curl -s localhost:8080/v1/tokens -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"name":"claude-code"}'
claude mcp add --transport http alerting http://localhost:8080/mcp --header "Authorization: Bearer <PAT>"
```

| Tool | Purpose |
| --- | --- |
| `list_alerts`, `get_alert` | scoped reads (runbook state, suggestion, deliveries, timeline) |
| `ack_alert`, `comment_alert` | act as the user |
| `raise_alert` | typed alert through the normal pipeline (e.g. `security.secret_leaked` pages Security) |
| `request_approval`, `get_approval_status` | ask a human; notifies approvers (UI + SMS) and waits up to the timeout |
| `run_runbook_step` | runs if low risk and trusted, else creates an approval |
| `propose_rule`, `test_rule`, `propose_schema_mapping`, `propose_runbook` | drafts, never saved |
| `get_integration_guide` + resource `docs://api` | guide + OpenAPI for coding agents |

The in-app copilot (`POST /v1/agent/chat`, SSE) uses the same tool implementations with an allow-list and drafts only. PATs are the hackathon shortcut; production target is OAuth 2.1 per the MCP spec. Details: [`backend/app/mcp/README.md`](backend/app/mcp/README.md).

## What is mocked

| Real | Mocked | Swap to real |
| --- | --- | --- |
| Ingest, queues, rules, dedup, escalation, approvals, SSE, MCP, metrics | | |
| Email over SMTP | Mailpit inbox | set `SMTP_HOST` (and credentials) to a real server |
| | SMS and phone calls (`mockgw`) | one adapter class (e.g. Twilio) + env, see channels README |
| | Workflow retry, service restart, failover, rollback, tickets (`mockgw`) | new / changed action class calling the real API |
| LLM calls (OpenAI / Anthropic / OpenRouter), local embeddings | | |
| JWT + personal access tokens | no SSO / OIDC | OIDC provider |
| | Customer systems (`simulator`, fictional "Vistula Capital") | real sources via webhook / RabbitMQ |
| **Not built** | Jira / Confluence live connectors (links are stored, not fetched live), GitHub / Datadog context in suggestions, KEDA / Kubernetes manifests, Rust hot-path | |

## Tests & CI/CD

- **Tests.** 845 backend pytest tests (plus mockgw, simulator and loadtest suites), ~97 % line coverage of `app/` (gate: `fail_under = 75`), against real Postgres / Redis / RabbitMQ (each test session gets its own database and queue/key prefix). Covers failure paths (broker 503, provider outage, invalid input, scope denial, tenant isolation, prompt injection). Loadtest and simulator have their own suites.
  ```bash
  make infra-up && make test     # backend suite with coverage
  make lint                      # ruff check + format check
  make loadtest-test             # report/reconcile unit tests, no infra needed
  ```
- **CI** (`.github/workflows/ci.yml`): lint, full suite with coverage on Postgres + pgvector, Redis, RabbitMQ and Mailpit services.
- **CD** (`.github/workflows/cd.yml`): push to `prod` → CI → `railway up` for api, workers, mockgw, simulator → smoke `GET /readyz`. Currently the Railway deploy was done from the CLI because GitHub Actions is blocked on the account used for this build (billing), see [`docs/MANUAL_STEPS.md`](docs/MANUAL_STEPS.md).

## AI usage & security disclosure

**How AI tools were used to build this.** The main Claude Code session (Claude Opus 5.5, high effort) acted as orchestrator: it planned phases, wrote task briefs and merged. Each task of [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) was implemented by an `implementer` subagent (Claude Sonnet 5.5, high effort) in its own git worktree, owning an exclusive set of files, with tests. An independent `qa-verifier` subagent ran lint, the full suite and compose smoke at each phase gate and fixed nothing. Architecture decisions, the spec and the contract-first workflow were set by the team; every agent change passed tests, the verifier and orchestrator review before merge. Definitions and rationale: [`docs/AGENTS.md`](docs/AGENTS.md), `.claude/agents/`.

**Security and privacy while building.** No real data anywhere: all events, users, phones and companies are synthetic ("Vistula Capital", [`docs/DEMO_ORG.md`](docs/DEMO_ORG.md)). Secrets only in git-ignored `.env`, GitHub secrets and Railway variables; `.env.example` has dev defaults and empty keys; agents were instructed never to print or commit keys. Agents ran locally under Claude Code permission modes.

**Security of the product.** Event content is untrusted (substitution-only templates, no Jinja / eval, HTML escaped in email, redaction + `<untrusted_event>` for models, injection detector). JWT + bcrypt, hashed personal tokens and source keys (SHA-256, shown once), Fernet-encrypted integration secrets, every read query filtered by role scope, grant rule (you can only grant roles ≤ yours within your scope), audit log, 256 KB payload limit, optional shared secret on the SMS callback. Demo gaps: no SSO, no mTLS between services, single-node infra.

## Repo map

```
backend/        Python 3.12 FastAPI service (one image, many roles)
  app/api/        routers (auto-discovered), SSE, MCP mount          app/pipeline/   ingest worker, mapping, rules, dedup
  app/delivery/   routing, channel adapters, retry/DLQ               app/escalation/ scheduler, on-call, policies, inbound SMS
  app/runbooks/   parsers, action registry, approvals, trust         app/ai/         LLM client, suggestions, copilot, redaction
  app/mcp/        MCP server + in-app copilot                        app/connectors/ RabbitMQ connector, runner
  migrations/     ordered .sql files        seed/  fixtures + loaders (Vistula Capital org)        tests/
mockgw/         fake SMS / phone / workflow / ticket gateway with chaos toggle
simulator/      "Vistula Capital" traffic generator
loadtest/       k6 scripts T1–T6, chaos script, report.py, results/
infra/          docker-compose (deps + full), Prometheus, Grafana, RAILWAY.md
docs/           spec, contract, plan, progress, architecture, demo org, agents
```

Docs: [`docs/BRIEF.md`](docs/BRIEF.md) (original brief) · [`docs/TECH_SPEC.md`](docs/TECH_SPEC.md) (source of truth) · [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/openapi.yaml`](docs/openapi.yaml) · [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) · [`docs/PROGRESS.md`](docs/PROGRESS.md) · [`docs/DEMO_ORG.md`](docs/DEMO_ORG.md) · [`docs/AGENTS.md`](docs/AGENTS.md) · [`docs/HANDOFF_UI.md`](docs/HANDOFF_UI.md) · [`loadtest/README.md`](loadtest/README.md) · [`infra/RAILWAY.md`](infra/RAILWAY.md)
