# Implementation Plan — gs-alerting (backend first)

Owner: orchestrator (Opus 5.5, effort high). Implementers: `implementer` agents (Sonnet, effort high), verification: `qa-verifier` (Sonnet, effort high).
Spec: `docs/TECH_SPEC.md` (wins on conflicts). Contract: `docs/openapi.yaml`. Status lives in `docs/PROGRESS.md`.

Scope of this plan: **backend, mocks, simulator, seed org, CI/CD, Railway, load tests.** The UI (`ui/`) is built separately and connected later; the backend must fully honour `openapi.yaml` so the UI can plug in.

---

## 0. How work flows

```
orchestrator ──(task brief: ID, files owned, interfaces, done-when)──► implementer (git worktree, own branch)
     ▲                                                                       │ commits on branch task/<ID>
     │                                                                       ▼
     └──── merge to main ◄── qa-verifier (runs tests + smoke on merged main) ◄┘
```

1. Orchestrator picks the next phase, spawns one implementer per task **in parallel** (isolated git worktree each).
2. Implementer works only inside its **owned files**, codes against the **interfaces** in §3, writes tests, runs `ruff` + its tests, commits to its branch, reports: what was done, files, test output, deviations, open issues.
3. Orchestrator merges branches into `main` (disjoint files ⇒ trivial merges), runs full suite, then a `qa-verifier` runs the phase gate (§5).
4. Gate green ⇒ PROGRESS.md updated, push, next phase. Gate red ⇒ fix task to the owning implementer (same agent via SendMessage if alive).

Rules for parallel safety:
- **File ownership is exclusive inside a phase.** Need a change in someone else's file? Don't. Write it in your report under "Requests for other owners"; the orchestrator integrates.
- **Migrations by number range** (§4). **Routers and seed loaders are auto-discovered** — no shared registry edits.
- Tests use a per-session database + key/queue prefix (conftest), so parallel test runs against the same local infra do not collide.

---

## 1. Phases overview

| Phase | Goal | Tasks (parallel inside phase) | Gate |
| --- | --- | --- | --- |
| P0 | Repo, docs, agents, GitHub, CI skeleton | orchestrator | repo pushed, CI file present |
| P1 | Foundation: runnable skeleton | P1-FOUND (1 agent) | `make infra-up && make test` green, `/healthz` ok, compose up works |
| P2 | Core pipeline end-to-end | P2-INGEST, P2-RULES, P2-DELIVERY, P2-ALERTSAPI | Appendix A events → alerts → email in Mailpit + SMS in mockgw + SSE; dedup ×500; coverage ≥ 70 % |
| P3 | Video features + measurement | P3-ESCAL, P3-RUNBOOK, P3-AI, P3-LOAD, P3-ORG | escalation in 30 s demo config; runbook + approval; AI suggestion (stub in tests); k6 T1/T4/T6 run with numbers |
| P4 | Agents + sources breadth | P4-MCP, P4-CONNECT | Claude Code via MCP raises alert & gets approval; RabbitMQ source + Alertmanager preset work |
| P5 | Ship | P5-DEPLOY (orchestrator), P5-PERF, P5-DOCS | Railway prod live via CD on `prod`; T2/T5 results; README complete |

Cut order if late (from spec §22): trust ladder → RabbitMQ connector → runbook text AI parse → rule-from-text → MCP `propose_*`. **Never cut:** no-loss, dedup, escalation, metrics, one-command start, load-test numbers.

---

## 2. Tasks

Each task lists: **Owns** (exclusive files), **Builds**, **Interfaces it must expose / may consume**, **Done when**. Implementers must read `CLAUDE.md`, `docs/TECH_SPEC.md` sections named in the task, and the relevant `openapi.yaml` schemas.

### P1-FOUND — Foundation (single agent; everything else depends on it)

Spec: §4 (all), §5.3, §15 conventions, §16 AuthN/AuthZ, §20, §21 users/domains/teams.

**Owns:** `backend/pyproject.toml`, `backend/uv.lock`, `backend/Dockerfile`, `backend/app/{__init__,main,config,roles}.py`, `backend/app/core/**`, `backend/app/schemas/**`, `backend/app/api/{__init__,app,deps}.py`, `backend/app/api/routers/{health,auth}.py`, `backend/app/pipeline/fields.py`, `backend/migrations/001_init.sql`, `backend/seed/{__init__,seed}.py`, `backend/seed/loaders/10_org.py`, `backend/seed/fixtures/org.yaml`, `backend/tests/conftest.py`, `backend/tests/fakes/**`, `backend/tests/core/**`, `infra/docker-compose.yml`, `infra/docker-compose.deps.yml`, `Makefile`, `.env.example`, `mockgw/` (stub only: `/healthz`, `/sms`, `/call` logging to memory).

**Builds:**
- `uv` project (Python 3.12): fastapi, uvicorn[standard], pydantic v2, pydantic-settings, orjson, asyncpg, pgvector, aio-pika, redis, pyjwt, bcrypt, cryptography, aiosmtplib, httpx, prometheus-client, pyyaml, uuid7 helper (`uuid6` pkg or own impl), litellm, fastembed, mcp; dev: pytest, pytest-asyncio, pytest-cov, ruff, datamodel-code-generator. Ruff config (line 110). Coverage config: `fail_under = 70` (raised to 75 in P5).
- `app/main.py --role api|worker|delivery|scheduler|ai|connector|all` and comma lists (`--role worker,delivery,scheduler,ai`). Role `api` runs uvicorn on `$PORT` (default 8080). Non-API roles run their `async def run(stop: asyncio.Event)` concurrently; graceful SIGTERM. `app/roles.py` maps role → `"module:function"` (lazy import). Create **stub modules** for every role target (`app/pipeline/worker.py`, `app/delivery/worker.py`, `app/escalation/scheduler.py`, `app/ai/worker.py`, `app/connectors/runner.py`) with a `run(stop)` that just waits on `stop` — later tasks replace their bodies (those files then belong to them).
- `app/config.py` Settings with every env var from spec §20 plus: `PORT`, `MQ_PREFIX` (default ""), `REDIS_PREFIX` (default ""), `DEMO_FAST_ESCALATION` (bool), `LOG_LEVEL`, `CORS_ORIGINS`, `ENVIRONMENT`.
- `app/core/`: `db.py` (asyncpg pool, jsonb codec via orjson, pgvector registration, migration runner with `schema_migrations` + advisory lock so N replicas can start together), `rabbit.py` (topology from spec §4.5 with prefix, publisher confirms, `publish`, `publish_batch`, `consume(queue, handler, prefetch)`), `redis.py`, `ids.py` (uuid7, short ids), `security.py` (bcrypt, JWT HS256, API key generation + sha256), `scope.py` (Scope model + loader + FastAPI deps: Bearer JWT, PAT, `?access_token=`), `outbox.py`, `audit.py`, `timeline.py`, `sse.py` (publish only), `metrics.py` (all Prometheus metrics in one place, names in §3.6), `logging.py` (JSON logs), `errors.py` (`{code,message,details}` handlers, 422 with field path).
- `app/schemas/api.py` generated from `docs/openapi.yaml` with datamodel-code-generator (pydantic v2, keep camelCase field names or aliases — consistent), plus `app/schemas/README.md` with the regenerate command. Hand-written internal models go to `app/schemas/internal.py`.
- `app/api/app.py` `create_app()`: lifespan opens pool/redis/rabbit, runs migrations (unless `SKIP_MIGRATIONS`), auto-includes every `app/api/routers/*.py` exposing `router`; ORJSONResponse default; CORS; `/metrics` mounted.
- Routers: `health.py` (`/healthz`, `/readyz` checks pg/redis/rabbit), `auth.py` (`POST /v1/auth/login`, `GET /v1/auth/me` per contract).
- `migrations/001_init.sql`: **the complete schema from spec §4.6** (all tables + indexes + `create extension vector`), plus `slug text unique` on `domains`, `teams` (wire ids accept slug or uuid), `users.timezone text default 'Europe/Warsaw'`, a fixed org uuid constant documented in `app/core/ids.py`.
- `seed/seed.py`: loads YAML fixtures, runs auto-discovered `seed/loaders/NN_*.py` in order inside one TX, idempotent (deterministic uuid5 ids from slugs/emails). `10_org.py` + `org.yaml`: users (password `demo`), domains, teams, memberships, role assignments from spec §21. CLI: `python -m seed.seed`.
- `tests/conftest.py`: session fixture creates DB `test_<random>`, runs migrations, sets `MQ_PREFIX`/`REDIS_PREFIX` to `t<random>.`, provides `pool`, `redis`, `rabbit`, `app` (httpx AsyncClient over ASGI), `seeded` (runs seed), `auth_headers(email)`, cleanup at end. `tests/fakes/llm.py` stub client.
- Docker: `backend/Dockerfile` (python:3.12-slim, uv sync --frozen, pre-download fastembed model in a layer, non-root, `CMD python -m app.main --role api`). `infra/docker-compose.deps.yml` (postgres pgvector/pgvector:pg16, redis:7, rabbitmq:3.13-management, mailpit, mockgw) and `infra/docker-compose.yml` (deps + api + worker + delivery + scheduler + ai + connector + migrate/seed one-shot + prometheus/grafana placeholders). Healthchecks + `depends_on: condition: service_healthy`.
- `Makefile` targets from CLAUDE.md (load-test targets may be placeholders until P3-LOAD).

**Done when:** `make infra-up && make test` green (tests for: migrations idempotent, rabbit publish+consume with confirm, JWT/scope loader, login/me, router auto-discovery, seed idempotent, fields.get_path); `make up` brings the stack up and `curl :8080/healthz` → ok; `ruff check` clean; `docker build backend` succeeds.

---

### P2-INGEST — Ingest, normalization, worker hot path

Spec: §4.4 (whole lifecycle), §5, §6 (deterministic part), §8 dedup/grouping, §13.4 detector, §18 metrics.

**Owns:** `app/api/routers/ingest.py`, `app/pipeline/{envelope,schema_detect,mapping,normalize,injection,fingerprint,worker}.py`, `app/pipeline/alert_writer.py`, `tests/pipeline/test_{ingest,schema_detect,mapping,injection,fingerprint,worker}*.py`, `scripts/demo_events.py` (+ `make demo-events`), `backend/seed/fixtures/samples/*.json` (Appendix A + injection sample + pricing log line).

**Builds:**
- `POST /v1/ingest/{sourceKey}`, `/batch` (≤1000, one confirm round-trip via `publish_batch`), `POST /v1/alerts/raise` (typed, `X-Api-Key`), `X-Api-Key` with `sourceKey=_`. Source lookup: in-process LRU/TTL 60 s cache over `sources.key_hash`. 256 KB limit → 413. RabbitMQ down → 503 + `Retry-After`. Priority from typed severity or source default. Metrics `ingested_total{source}`.
- Worker (`app/pipeline/worker.py:run`): consume `events.in` prefetch 200, process with bounded concurrency; steps exactly as spec §4.4.2: Redis idempotency `evt:{id}`, `recent:{source_id}` stream (MAXLEN ~5000), schema detect, mapping → `_meta`, injection flags, `RuleIndex.candidates` → `CompiledRule.matches`, render title/body/fields, fingerprint (+ grouping window), **one TX** in `alert_writer.upsert_alert(...)` (upsert with `ON CONFLICT … WHERE status <> 'resolved'`, `(xmax = 0) AS inserted`, grouped_items append cap 100, timeline, `knowledge_key`, and on new alert call `routing.plan_deliveries(...)` + outbox rows `ai.tasks` and SSE), after commit publish outbox via `core.outbox`, SSE `alert.created|updated` (count changes throttled to max 1 SSE per alert per 250 ms), ack. Outcome counters (`alerted_new|deduplicated|grouped|unrouted|invalid`) + `processing_latency_seconds` histogram (commit − received_at).
- Rule hot reload: worker calls `RuleIndex.start_hot_reload()` (from P2-RULES).
- `scripts/demo_events.py` sends Appendix A samples + injection log line to seeded sources.

**Consumes:** `app.pipeline.rules.index.RuleIndex`, `app.pipeline.rules.templates.render`, `app.delivery.routing.plan_deliveries`, core modules. Until siblings merge, code against the signatures in §3 and use small local fakes in tests.

**Done when:** unit tests for detect/mapping/injection/fingerprint; integration test: publish 500 identical events → 1 alert `count=500`, exactly one set of hard deliveries; batch endpoint 1000 events → 202; idempotency (same event_id twice → processed once); unrouted counted.

---

### P2-RULES — Rule engine, rules API, sources API

Spec: §6 (mapping storage/API), §7 (all), §5.2 presets (data only), §16 source keys.

**Owns:** `app/pipeline/rules/**` (`model.py, compiler.py, index.py, templates.py, dryrun.py`), `app/api/routers/{rules,sources}.py`, `app/repositories/{rules,sources}.py`, `seed/loaders/{20_sources,30_rules}.py`, `seed/fixtures/{sources,rules}.yaml`, `tests/rules/**`, `tests/api/test_{rules,sources}*.py`.

**Builds:**
- Rule model = contract `Rule` (+ condition groups nested AND/OR, operators of §7.2, regex 50 ms guard via pre-compile + length cap, glob, exists). Compiler → closure tree; `RuleValidationError(path, message)` → 422.
- `RuleIndex` (§7.3): index on top-level AND `eq` of `_source|_schema|_type`, wildcard list; `load_all`, `reload_rule`, `start_hot_reload` (Redis pub/sub `rules:changed`, reload ≤ 1 s); thread/async safe swap.
- Templates (§7.4): `{{path}}` only, HTML-escape flag, 500-char truncation.
- Rules API: list/create/get/put/delete, versions (`rule_versions`), YAML get/put (round-trip), dry-run (§7.5 over `recent:*` streams of team sources), `from-text` endpoint **stub that returns 503 `ai_unavailable`** (P3-AI replaces the handler body in `app/ai/rule_copilot.py`; route calls `app.ai.rule_copilot.draft_rule` if importable).
- Sources API: list/create (returns key once), rotate-key, sample-events (from `recent:{id}`), `PUT mapping`, snippets (curl/python/js), mapping `suggest` route calls `app.ai.mapping.suggest_mapping` (P3-AI) else heuristic fallback (implement the deterministic heuristic here in `app/pipeline/rules/../mapping_heuristic.py` — owned by this task: `app/pipeline/mapping_heuristic.py`).
- Seed: sources from spec §21 with **fixed demo keys** (`demo-key-workflow-broker`, `demo-key-prometheus`, `demo-key-pricing-logs`, `demo-key-pr-review-agent`, `demo-key-trade-gateway`), and the four rules from spec §7.1 table.
- All reads scoped.

**Done when:** operator unit tests (every operator, numeric vs string, nested AND/OR, missing paths), index candidate tests, template tests (escaping, truncation, no code exec), API tests for CRUD/versions/YAML/dry-run/scope denial, hot reload test (update rule → index sees new version < 2 s).

---

### P2-DELIVERY — Routing, delivery workers, channels, mockgw

Spec: §9 (all), §10.1 (effective policy lookup, minimal), §10.3 (on-call lookup, minimal), §8 throttling, §4.5 retry topology.

**Owns:** `app/delivery/**` (`routing.py, worker.py, retry.py, channels/{base,registry,ui,email,sms,phone,webhook}.py, render.py`), `app/escalation/{policies,oncall}.py` (minimal versions; P3-ESCAL extends them), `app/core/outbox_relay.py` (relay loop function used by scheduler), `seed/loaders/40_policies.py`, `seed/fixtures/policies.yaml`, `mockgw/**` (full), `tests/delivery/**`, `tests/mockgw/**`.

**Builds:**
- `routing.plan_deliveries(conn, alert, rule, reason)` (§9.1/9.2): resolve hard/soft recipients (oncall/user/team/role; slug or uuid), channel selection order (timeWindow → channelsBySeverity → user prefs + working hours → policy level channels), always a `ui` delivery, idempotency key `{alert_id}:{seq}:{user}:{channel}`, throttle `rl:{recipient}:{channel}:{minute}` → `suppressed_digest`; inserts `deliveries` + outbox rows; sets `ack_deadline`/`next_renotify_at` on the alert from effective policy (respect `DEMO_FAST_ESCALATION` → minutes become seconds).
- `policies.effective_policy(conn, *, rule, team_id, domain_id, severity) -> EffectivePolicy` (rule → team → domain → org per-field merge, with origin per field). `oncall.current_oncall(conn, team_id, at) -> list[UUID]` (shift precedence override > manual > rotation, fallback all team members).
- Delivery worker consumes `deliveries.{ui,email,sms,phone,webhook}`: idempotent skip, 3 s timeout, success → status + `delivered_at` + timeline + SSE `delivery.status` + `delivery_latency_seconds{channel}`; failure → retry queues 5s/30s/120s → after 3 → DLQ + fallback channel (`phone→sms→email→ui`) + timeline "SMS gateway down, fell back to email".
- Adapters: `ui` (SSE), `email` (aiosmtplib → Mailpit, HTML with runbook placeholder section + ack/resolve deep links), `sms` (≤160 chars format from spec), `phone`, `webhook` (HMAC). Registry: new provider = one class + `@register`.
- mockgw (FastAPI, own Dockerfile): `/sms`, `/call` (Idempotency-Key honoured), `/admin/fail {channel, seconds}` → 503, HTML log page (auto-refresh) showing SMS/calls with "reply" form → POSTs to backend `/v1/inbound/sms`, `/workflows/{id}/retry`, `/services/{name}/restart`, `/tickets`, `/svc/{name}/health`, `/failover`, `/rollback`; `/admin/state` JSON for tests.
- Outbox relay function `relay_once(pool, rabbit)` (rows unpublished > 5 s).

**Done when:** tests: channel selection matrix (Anna time window, severity map, prefs), on-call precedence, retry→DLQ→fallback with mockgw failing, idempotent re-delivery, throttle suppression, email arrives in Mailpit (via Mailpit API), mockgw unit tests.

---

### P2-ALERTSAPI — Alerts REST, SSE stream, org API, metrics summary

Spec: §15 (Alerts, Org, Realtime & metrics), §16, §18 latency definitions.

**Owns:** `app/api/routers/{alerts,org,stream,metrics_summary}.py`, `app/repositories/{alerts,org}.py`, `tests/api/test_{alerts,org,stream,metrics,isolation}*.py`.

**Builds:**
- `GET /v1/alerts` (filters status/severity/team/domain/source/q, cursor pagination, default my teams & not resolved), `GET /v1/alerts/{id}` (`AlertDetail`: deliveries, timeline, runbook placeholder, suggestion placeholder, escalation countdown fields), `ack` (clears deadlines, SSE, audit, timeline), `resolve {resolutionNote}` (writes `resolutions` row with knowledge_key, SSE, soft "resolved" updates via `routing` if available), `assign`, `comments`. Accept `A-1234` short ids and uuids.
- `GET /v1/stream` SSE: auth via `access_token`, subscribes `sse:team:{id}` for teams in scope + `sse:user:{id}`, heartbeat 15 s, `Last-Event-ID` tolerant; uses `redis.asyncio` pub/sub; one subscription per connection.
- Org: `GET /v1/domains|teams|users`, `POST /v1/users/{id}/roles` (grant rule §16, 403 otherwise, audit).
- `GET /v1/metrics/summary` (contract `MetricsSummary`: ingested, outcomes, queue depths via RabbitMQ management API or passive declare, p50/p99 processing + delivery from DB `percentile_cont` over last N minutes).
- Isolation test suite (NFR-7): user from another domain gets 404/403 on alerts/rules/sources.

**Done when:** API tests per endpoint incl. scope denial, SSE test receives `alert.created` after a publish to Redis, metrics summary test with synthetic deliveries.

---

### P3-ESCAL — Escalation, on-call, preferences, SMS replies

Spec: §10 (all), §5.1 inbound SMS, §11.4 SMS approvals hook.

**Owns:** `app/escalation/{scheduler,rotations,jobs}.py`, extends `app/escalation/{policies,oncall}.py` (ownership transfers from P2-DELIVERY), `app/api/routers/{schedules,escalation,preferences,inbound}.py`, `seed/loaders/50_rotations.py`, `seed/fixtures/rotations.yaml`, `tests/escalation/**`.

**Builds:** scheduler loop 1 s with `FOR UPDATE SKIP LOCKED` jobs: escalation (level++, next level recipients oncall → domain_root → master → custom, hard deliveries + soft "escalated" updates, SSE `alert.escalated`), renotify, outbox relay (`relay_once`), approval expiry, shift materialisation (14 days), digests (5 min, suppressed deliveries → one email). Policy CRUD + effective endpoint with origins. Rotations/schedules/oncall endpoints. Preferences me GET/PUT. `POST /v1/inbound/sms` parsing `ACK|RESOLVE|APPROVE|REJECT <id>` by sender phone (approvals call `app.runbooks.approvals.decide` from P3-RUNBOOK).

**Done when:** test with `DEMO_FAST_ESCALATION`: unacked critical escalates to Domain Root then Master; ack stops it; two scheduler instances don't double-escalate; SMS `ACK 1234` acks.

### P3-RUNBOOK — Runbooks, actions, approvals, trust

Spec: §11 (all), §2.6 brief.

**Owns:** `app/runbooks/**` (`model, lookup, render, parsers/{yaml,markdown,text,link}, actions/{registry,http_get,retry_workflow,restart_service,webhook,open_ticket,escalate,failover_region,rollback_deploy}, runner, approvals, trust`), `app/api/routers/{runbooks,approvals,action_trust,runbook_steps}.py`, `seed/loaders/60_runbooks.py`, `seed/fixtures/runbooks/*` (the three from Appendix B), `tests/runbooks/**`.

**Builds:** deterministic YAML + Markdown parsers → `RunbookStep`; text/link parsers (text calls `app.ai.runbook_parse.parse_text` if available, else single "instruction" step); attachment lookup rule → schema/type; templated rendering from alert raw/_meta (SMS = short link); action registry `@register`; step run endpoint (low risk runs; high/approval → approval row + hard notification to approvers + SSE `approval.requested`); approvals list/decision (`decide()` function reused by SMS + MCP), expiry; `step_runs` + timeline + audit; action trust endpoints + promotion/demotion bookkeeping (wave-3 logic may be minimal). Alert detail runbook section: expose `app.runbooks.lookup.runbook_for_alert(conn, alert)` (P2-ALERTSAPI detail calls it if present — orchestrator wires).

**Done when:** parser tests on all three Appendix B samples + one unseen format; settlement runbook: http_get runs, retry_workflow runs (mockgw), escalate requires approval → approve → runs; audit rows present.

### P3-AI — LLM layer, suggestions, knowledge

Spec: §12, §13 (all), §6 AI mapping, §7.6.

**Owns:** `app/ai/**` (`llm.py` LiteLLM Router OpenAI/Anthropic/OpenRouter + fallback + JSON-schema output + one repair retry, `redact.py`, `prompts/`, `embeddings.py` fastembed, `retrieval.py` L1 exact + L2 hybrid RRF, `suggest.py`, `worker.py`, `mapping.py`, `rule_copilot.py`, `runbook_parse.py`, `resolution_note.py`), `app/knowledge/**`, `app/api/routers/{knowledge,integrations,suggestions}.py`, `tests/ai/**`, `tests/fakes/llm.py` (extend).

**Builds:** everything in §13 with "AI unavailable" degradation, per-domain LLM config (Fernet), suggestion pipeline consumed from `ai.tasks`, SSE `suggestion.ready`, suggestion decision endpoint, knowledge upload (markdown/text/url) → chunks → embeddings, resolution → knowledge (fixed-before). Prompts keep event content in `<untrusted_event>`; output constrained to existing runbook step ids.

**Done when:** tests with stub LLM: redaction patterns, suggestion validation drops invented step ids, fixed-before L1 hit after a resolution, injection-flagged alert yields no action suggestion, provider fallback order honoured (fake router), mapping drops non-existent paths.

### P3-LOAD — Load tests, chaos, report

Spec: §18, §19.

**Owns:** `loadtest/**`, Makefile `t1 t2 t4 t5 t6 report` targets (append-only block marked `# --- loadtest ---`), `infra/prometheus.yml`, `infra/grafana/**`.

**Builds:** k6 scripts (T1 constant 100 ev/s 5 min mixed Appendix A, 20 % matching; T2 ramping arrival via batch endpoint until p99 > 1 s or errors; T6 500 identical in 60 s), `t4_chaos.sh` (mockgw fail sms 60 s + `docker kill` a worker during 200 ev/s, then reconciliation SQL: ingested = Σ outcomes; deliveries = delivered + dead-lettered-with-fallback + suppressed ⇒ **lost = 0**), `t5_scaling.sh` (T2 at 1/2/4 workers), `report.py` (SQL percentiles + Prometheus → `loadtest/results/<date>/*.md` + PNG charts). Short durations configurable via env for CI smoke. Grafana dashboard "Alerting – load" (ingest rate, outcomes, queue depth, p50/p99).

**Done when:** scripts run locally against `make up` and produce a results markdown with p50/p99 + throughput; CI has a 30 s smoke variant.

### P3-ORG — "Vistula Capital" mock organization + simulator

**Owns:** `seed/fixtures/org.yaml` (enrich — ownership transfers from P1), `seed/fixtures/history.yaml`, `seed/fixtures/knowledge/*.md`, `seed/loaders/90_history.py`, `simulator/**`, `docs/DEMO_ORG.md`.

**Builds:**
- Fictional bank **Vistula Capital** (no real company names): 4 domains (Payments, Settlements, Security, Markets), 6 teams (+ cross-domain "Checkout Project"), ~25 users with roles, phones (+48 500 1xx xxx fake), timezones, preferences, working hours; keeps all spec §21 demo users and emails unchanged.
- History: ~40 resolved alerts over the last 30 days with resolution notes (fixed-before demo), 6 knowledge docs (post-mortems, wiki pages) as Markdown.
- `simulator/` service (Python, own Dockerfile): Vistula Capital's systems (`order-settlement` workflow engine → publishes to its own RabbitMQ queue consumed by our connector, or webhook fallback; `payments-api` Prometheus Alertmanager webhooks; `pricing-engine-1..4` log lines; `trade-gateway` business events; `pr-review-agent` typed raises). Traffic shaped by time-of-day with baseline rate env-configurable; scenario endpoints `POST /scenario/{settlement_outage|payments_error_spike|pricing_latency|trade_limit_breach|secret_leaked|poisoned_log|storm_500}`; small HTML control panel; `/stats`.

**Done when:** seed loads full org idempotently; simulator runs in compose and each scenario produces the expected alert(s) end-to-end (integration test or scripted check); DEMO_ORG.md lists people, teams, scenarios.

### P4-MCP — MCP server + in-app agent

Spec: §14. **Owns:** `app/mcp/**`, `app/api/routers/{agent,tokens}.py`, `tests/mcp/**`. Builds FastMCP mounted at `/mcp` (PAT auth), all tools from §14 reusing service functions (no duplicated logic), `request_approval` long-poll on `appr:wait:{id}`, resource `docs://api`, PAT create/list/revoke endpoints (add to `openapi.yaml` first), `POST /v1/agent/chat` SSE tool loop with `LLM_MODEL_SMART`. **Done when:** MCP client test (official SDK client over HTTP) lists tools, raises alert, request_approval resolves after decision.

### P4-CONNECT — RabbitMQ source connector + presets

Spec: §5.2. **Owns:** `app/connectors/**`, `app/pipeline/presets/**`, `tests/connectors/**`. Builds connector role (one consumer per `rabbitmq` source, reconnect with backoff, republish into `events.in` with source id), presets alertmanager (split `alerts[]`, `status: resolved` → auto-resolve), sentry, datadog, cloudevents (pre-filled mappings). **Done when:** test publishes to a customer queue → alert; Alertmanager resolved payload resolves the alert.

### P5-DEPLOY (orchestrator) · P5-PERF · P5-DOCS

- **P5-DEPLOY:** Railway project `gs-alerting`, env `production`: postgres (pgvector image + volume), redis, rabbitmq (+volume), mailpit, mockgw, `api` (role api), `workers` (role `worker,delivery,scheduler,ai,connector`), `simulator`. GitHub Actions CD on push to `prod`: reuse CI, then `railway up` per service with `RAILWAY_TOKEN`. Smoke test after deploy (`/readyz`, one ingest → alert).
- **P5-PERF:** profile under T2, fix hot spots (batch ingest, prefetch, Redis-coalesced counters if row contention), run T1/T2/T4/T5/T6, commit results + charts, targets from spec §18 met or explained.
- **P5-DOCS:** README (quick start ≤ 3 commands, architecture, what is mocked, results table, AI usage & security disclosure, MCP connect snippet), coverage ≥ 75 %.

---

## 3. Interfaces between modules (contracts for parallel work)

Signatures are binding. Change only via orchestrator.

### 3.1 Core (P1)
```python
# app/config.py
def get_settings() -> Settings
# app/core/db.py
async def create_pool(dsn: str | None = None) -> asyncpg.Pool
async def run_migrations(pool: asyncpg.Pool) -> list[str]          # returns applied filenames
# app/core/rabbit.py
class Rabbit:
    async def connect(self) -> None
    async def declare_topology(self) -> None
    def name(self, base: str) -> str                                 # applies MQ_PREFIX
    async def publish(self, exchange: str, routing_key: str, body: dict | bytes, *, priority: int = 0, headers: dict | None = None) -> None   # awaits confirm
    async def publish_batch(self, exchange: str, routing_key: str, bodies: list[dict], *, priority: int = 0) -> None
    async def consume(self, queue: str, handler: Callable[[aio_pika.IncomingMessage], Awaitable[None]], *, prefetch: int = 200) -> None
# app/core/redis.py
def get_redis() -> redis.asyncio.Redis ;  def rkey(*parts: str) -> str   # applies REDIS_PREFIX
# app/core/scope.py
class Scope(BaseModel): user_id: UUID; is_master: bool; domain_ids: set[UUID]; team_ids: set[UUID]; roles: list[RoleAssignment]
async def load_scope(conn, user_id: UUID) -> Scope
CurrentUser = Annotated[User, Depends(current_user)] ; CurrentScope = Annotated[Scope, Depends(current_scope)]
# app/core/outbox.py
async def add_outbox(conn, topic: str, routing_key: str, payload: dict, priority: int = 0) -> int
async def publish_outbox_rows(pool, rabbit, ids: list[int]) -> None
# app/core/sse.py
async def publish_sse(event_type: str, data: dict, *, team_ids: Iterable[UUID] = (), user_ids: Iterable[UUID] = ()) -> None
# app/core/timeline.py / audit.py
async def add_timeline(conn, alert_id: UUID, type: str, text: str, *, actor: dict | None = None, data: dict | None = None) -> None
async def audit(conn, actor: dict, action: str, target_type: str, target_id: str, data: dict | None = None) -> None
# app/pipeline/fields.py
MISSING = object()
def get_path(obj: Any, path: str) -> Any                                   # dotted + numeric index, MISSING→None
@dataclass class EventView: raw: dict; meta: dict; def get(self, field: str) -> Any   # "_x" → meta["x"], else raw path
```
Actor dicts: `{"type": "user"|"agent"|"system", "id": str|None, "name": str, "onBehalfOf": str|None}`.

### 3.2 Rules (P2-RULES → consumed by P2-INGEST)
```python
# app/pipeline/rules/model.py
class RuleDef(BaseModel)          # mirrors contract Rule; .id, .team_id, .match, .routing, .presentation, .dedup, .grouping, .throttle, .runbook_id, .priority
# app/pipeline/rules/compiler.py
class CompiledRule: rule: RuleDef; def matches(self, ev: EventView) -> bool
def compile_rule(rule: RuleDef) -> CompiledRule                     # raises RuleValidationError(path, message)
# app/pipeline/rules/index.py
class RuleIndex:
    async def load_all(self, pool) -> None
    async def reload_rule(self, pool, rule_id: UUID) -> None
    def candidates(self, ev: EventView) -> list[CompiledRule]
    async def start_hot_reload(self, pool) -> asyncio.Task
# app/pipeline/rules/templates.py
def render(template: str, ev: EventView, *, html: bool = False, max_len: int = 500) -> str
```

### 3.3 Routing (P2-DELIVERY → consumed by P2-INGEST, P3-ESCAL, P2-ALERTSAPI)
```python
# app/delivery/routing.py
async def plan_deliveries(conn, alert: dict, rule: RuleDef | None, *, reason: Literal["created","escalated","renotify","resolved","severity_up","reopened"], level: int = 1) -> list[dict]
    # inserts deliveries + outbox rows in caller's TX; updates alert ack_deadline/next_renotify_at when reason in created/escalated
# app/escalation/policies.py
async def effective_policy(conn, *, rule: RuleDef | None, team_id: UUID, domain_id: UUID, severity: str) -> EffectivePolicy
# app/escalation/oncall.py
async def current_oncall(conn, team_id: UUID, at: datetime | None = None) -> list[UUID]
```

### 3.4 Alert writer (P2-INGEST)
```python
# app/pipeline/alert_writer.py
async def upsert_alert(conn, *, rule: CompiledRule, ev: EventView, envelope: dict, rendered: RenderedAlert, fingerprint: str) -> UpsertResult  # (alert_id, inserted: bool, count, outbox_ids)
```

### 3.5 Runbooks / approvals / AI (P3, consumed by P2-ALERTSAPI wiring, P3-ESCAL, P4-MCP)
```python
async def runbook_for_alert(conn, alert: dict) -> RunbookView | None                 # app/runbooks/lookup.py
async def decide(conn, approval_id: UUID, *, decision: Literal["approve","reject"], actor: dict, comment: str | None) -> dict   # app/runbooks/approvals.py
async def request_approval(conn, *, origin, action, parameters, reason, risk, alert_id, requested_by) -> dict
async def suggest_mapping(sample: dict, source: dict) -> SchemaMapping              # app/ai/mapping.py
async def draft_rule(text: str, scope: Scope, conn) -> tuple[RuleDef, str]          # app/ai/rule_copilot.py
async def parse_text(text: str) -> list[RunbookStep]                                 # app/ai/runbook_parse.py
```

### 3.6 Metric names (P1 defines in `app/core/metrics.py`)
`alerting_ingested_total{source}`, `alerting_events_outcome_total{outcome=alerted_new|deduplicated|grouped|unrouted|invalid|auto_resolved}`, `alerting_processing_latency_seconds` (histogram), `alerting_delivery_latency_seconds{channel}` (histogram), `alerting_deliveries_total{channel,status}`, `alerting_rule_eval_seconds` (histogram), `alerting_sse_clients`, `alerting_outbox_unpublished`.

Non-API roles expose their metrics with `prometheus_client.start_http_server(METRICS_PORT)` (default **9101**); the api serves `/metrics` on its HTTP port. Prometheus scrapes both (`infra/prometheus.yml`).

---

## 4. Migrations number ranges
| Range | Owner |
| --- | --- |
| 001–009 | P1-FOUND |
| 010–019 | P2-INGEST |
| 020–029 | P2-RULES |
| 030–039 | P2-DELIVERY |
| 040–049 | P2-ALERTSAPI |
| 050–059 | P3-ESCAL |
| 060–069 | P3-RUNBOOK |
| 070–079 | P3-AI |
| 080–089 | P4-MCP / P4-CONNECT |
| 090–099 | P3-ORG / P5 |

## 5. Phase gates (qa-verifier checklist)

Each gate: `make lint`, `make test` (coverage report attached), `docker compose -f infra/docker-compose.yml up -d --build`, then the phase smoke:

- **P1:** `/healthz`, `/readyz` ok; login as `anna@demo` / `demo` returns token; `/v1/auth/me` scopes correct.
- **P2:** `make demo-events` → 4 alerts per spec rules (Settlements, Rates, Anna, Pricing grouped); Mailpit has the Rates email; mockgw shows SMS/phone for Settlements; `curl -N /v1/stream` shows `alert.created`; T6 dedup: 500 events → 1 alert ×500; SMS outage → fallback email.
- **P3:** escalation in fast mode; runbook step + approval; AI suggestion with real key if `.env` has one (else "AI unavailable" degradation visible); `make t1 t4 t6` produce results with lost = 0.
- **P4:** MCP test client; RabbitMQ source end-to-end; Alertmanager resolve.
- **P5:** Railway URLs healthy; CD run green on `prod`; README fresh-clone test (time it).

## 6. Definition of done (whole backend)
- Every path in `openapi.yaml` implemented or explicitly returns 501 with a note in PROGRESS.md.
- Coverage ≥ 75 % (`backend/app`), CI green on `main`, CD green on `prod`.
- Load test results committed (`loadtest/results/`) with p50/p99 and max sustained ev/s; T4 lost = 0.
- README answers: how to run (≤ 3 commands), what is mocked, how to add source/channel/rule/action, results, AI usage disclosure.
