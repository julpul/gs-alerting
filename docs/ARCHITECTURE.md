# Architecture (as built)

Companion to [`TECH_SPEC.md`](TECH_SPEC.md) (design, source of truth) and the [README](../README.md). This page describes what the code does today: event lifecycle and guarantees, queue topology, Redis keys, tables, delivery semantics, failure modes.

## 1. Components

One image (`backend/`), several roles started with `python -m app.main --role <role>` (`APP_ROLE`; a comma list or `all` runs several roles in one process).

| Role | Consumes / drives | Writes |
| --- | --- | --- |
| `api` | HTTP: ingest, REST, SSE, MCP | publishes `events.in`, `ai.tasks`; Postgres via REST |
| `worker` | `events.in` | alert + deliveries + outbox (1 TX), Redis (idempotency, recent stream, stats), SSE |
| `delivery` | `deliveries.{ui,email,sms,phone,webhook}` | delivery status, retries, DLQ, fallback deliveries |
| `scheduler` | 1 s loop, `FOR UPDATE SKIP LOCKED` | escalation, renotify, outbox relay, shift generation, digest, approval expiry |
| `ai` | `ai.tasks` | `suggestions`, embeddings, resolution notes |
| `connector` | customer RabbitMQ queues (`sources.type = rabbitmq`) | `events.in` (with publisher confirm) |

Non-API roles expose Prometheus metrics on `:9101`; the API on `:8080/metrics`.

## 2. Event lifecycle and guarantees

1. **Ingest (`api`).** Resolve the source by API key (in-memory cache, 60 s, backed by Postgres) → validate JSON (≤ 256 KB) → assign `event_id` (UUIDv7) and `received_at` (server UTC, ms) → publish the envelope to exchange `events` (persistent, priority from the source default or typed severity) → **wait for the publisher confirm** → `202 {eventId}`. Broker down → `503 Retry-After`: an event is never accepted unless it is persisted. Webhook batch (`/batch`, up to 1000 events) uses one confirm round trip.
2. **Worker** consumes `events.in` (prefetch 200, manual ack):
   1. **Claim.** `SET evt:{id} p NX EX 300`. Already claimed → skip. A *redelivered* message may take over a claim still marked in-flight (`p`) by a crashed worker. After processing the key is rewritten as done (`1`, 1 h) in the same Redis pipeline as the stats and the recent-event stream (one Redis round trip per event).
   2. Push the raw event to the capped stream `recent:{source_id}`.
   3. **Normalize** with the source mapping → `_meta` (`source, schema, severity, service, environment, type, title, timestamp, error_code, flags`).
   4. **Injection detector** (deterministic regex scoring) → `flags += suspicious_content`.
   5. **Presets**: Alertmanager `status: resolved` resolves the matching open alert (`auto_resolved`) and creates none.
   6. Candidate rules from the in-memory index → evaluate. One event may match several teams' rules. Per match: render title / body / fields, compute fingerprint, then **one Postgres transaction**: upsert alert (`ON CONFLICT (rule_id, fingerprint) WHERE status <> 'resolved'` → `count + 1`), grouped item, and, only if the alert is new or changed state, `deliveries` + `timeline` + `outbox` rows (`deliveries`, `ai.tasks`, SSE).
   7. After commit: publish outbox rows (confirm) and mark `published_at`. If this step fails, the **scheduler's outbox relay** republishes rows with `published_at IS NULL` older than 5 s.
   8. Publish SSE (`alert.created` / `alert.updated`) to Redis pub/sub (throttled per alert).
   9. **Ack** the RabbitMQ message. Every event ends in exactly one outcome counter: `alerted_new`, `deduplicated`, `grouped`, `unrouted`, `invalid`, `auto_resolved`, so `ingested = Σ outcomes` (the UI's `lost` is derived from this and must be 0).
3. **Delivery** consumes `deliveries.{channel}`: skip if already terminal, send via the channel adapter (3 s timeout), update status, SSE `delivery.status`.
4. **Scheduler** escalates unacknowledged hard alerts, renotifies, relays the outbox, sends digests (see [spec §10](TECH_SPEC.md)).
5. **AI worker** builds the suggestion from `ai.tasks`. The hot path never waits for it and never calls a model.

**Guarantees.** At-least-once end to end, made effectively-once by three idempotency layers: event (Redis claim), delivery (unique `deliveries.idempotency_key` in Postgres plus the status check before sending), provider (`Idempotency-Key` header, honoured by mockgw). A crash before the ack means redelivery, and the claim logic above makes the retry safe. Infrastructure errors (Postgres, Redis down) requeue the message; an event that fails twice for a non-infrastructure reason is counted `invalid` and acked, so a poison message cannot loop.

## 3. RabbitMQ topology

| Exchange (type) | Queue | Args | Producer → consumer |
| --- | --- | --- | --- |
| `events` (direct) | `events.in` | durable, `x-max-priority: 10` | api, connector → worker |
| `deliveries` (direct, key = channel) | `deliveries.ui`, `.email`, `.sms`, `.phone`, `.webhook` | durable, `x-max-priority: 10` | worker, scheduler (outbox) → delivery |
| `deliveries.retry` (direct) | `deliveries.retry.5s`, `.30s`, `.120s` | `x-message-ttl`; dead-letter back to `deliveries` (routed to the channel queue via the `CC` header) | delivery (on failure) |
| `deliveries.dlq` (fanout) | `deliveries.dlq` | durable | delivery after 3 failed attempts |
| `ai` (direct) | `ai.tasks` | durable | worker (outbox), api → ai |

Priority mapping: critical 9, error 6, warning 3, info 0. All names get an optional `MQ_PREFIX` (tests). Demo uses single-node classic queues (full priority support); production note: quorum queues on a 3-node cluster.

## 4. Redis keys

All keys carry the optional `REDIS_PREFIX`.

| Key | Type | Purpose |
| --- | --- | --- |
| `evt:{event_id}` | string, `p` EX 300 → `1` EX 3600 | worker idempotency claim |
| `recent:{source_id}` | stream, capped (~5000) | sample events, dry-run, unrouted view |
| `rules:changed` | pub/sub | hot reload of the rule index in every worker |
| `sse:team:{id}`, `sse:user:{id}` | pub/sub | SSE fan-out across api replicas |
| `sse:throttle:{alert_id}` | string, short PX | throttles repeated `alert.updated` pushes |
| `rl:{recipient}:{channel}:{minute}` | counter, EX 120 | per-recipient throttle |
| `stats:*` | hashes | cross-process counters behind `/v1/metrics/summary` (mirror of Prometheus, survives SIGKILLed workers) |
| `appr:wait:{approval_id}` | pub/sub | wakes the MCP `request_approval` long-poll |

## 5. Postgres (migrations `backend/migrations/`, applied in filename order at startup)

| Area | Tables |
| --- | --- |
| Identity and org | `users`, `domains`, `teams`, `team_domains`, `team_members`, `role_assignments`, `personal_tokens` |
| Sources and rules | `sources` (key hash, mapping, preset, config), `rules` (+ versions), `escalation_policies`, `preferences` |
| Alerts | `alerts` (partial unique index `(rule_id, fingerprint) WHERE status <> 'resolved'`), `timeline`, `audit_log` |
| Delivery | `deliveries` (unique `idempotency_key`, `event_received_at` for latency), `outbox` (partial index on unpublished) |
| On-call | `rotations`, `shifts` |
| Runbooks | `runbooks`, `step_runs`, `approvals`, `action_trust` |
| AI and memory | `suggestions`, `resolutions` (index on `domain_id, knowledge_key`), knowledge documents / chunks with pgvector embeddings and full-text index |

Individual events are deliberately **not** stored ([spec §5.4](TECH_SPEC.md)). Every user-visible read takes the caller's scope and filters by it; there is no unscoped query helper outside seed, admin and workers.

## 6. Delivery semantics

- **Plan.** On alert creation / escalation / re-open / severity increase / resolve the worker (or scheduler) calls `plan_deliveries`: hard recipients (on-call, then up the pyramid) and soft recipients (informed), channel chosen by rule time window → rule severity map → user preferences → effective escalation policy. A UI delivery is always created.
- **Outbox.** Delivery messages are written to `outbox` in the same transaction as the alert, then published after commit; the relay closes the gap if the process dies in between. This is what makes "alert committed" imply "notification will be attempted".
- **Send.** Load the row; if `sent | delivered | acked | dead_lettered | suppressed_digest | failed` → ack and skip. Otherwise send with a 3 s timeout.
- **Failure.** `attempts + 1` → `deliveries.retry.5s` (1st), `.30s` (2nd) → after 3 failures `deliveries.dlq`, status `dead_lettered` and a **fallback delivery** on the next channel (`phone → sms → email`) with a timeline entry ("SMS gateway down, fell back to email").
- **Throttle.** Over `maxPerRecipientPerMinute` the delivery is `suppressed_digest`; the scheduler later sends one digest email and marks them `digested`.
- **Acknowledgement.** Ack (UI, SMS reply `ACK <id>`, phone key 1, MCP) sets hard deliveries to `acked`, clears `ack_deadline` and `next_renotify_at`, stopping escalation.

## 7. Failure modes

| Failure | Behaviour | Evidence |
| --- | --- | --- |
| RabbitMQ down | Ingest answers `503 Retry-After` (nothing is accepted that is not persisted); producers retry. Workers and delivery reconnect (aio-pika robust connection) | ingest tests |
| Channel / gateway down (SMS, phone) | Retry 5 s / 30 s, DLQ after 3 attempts, fallback delivery on the next channel; outbox rows already committed | T4: SMS outage 60 s, 0 lost |
| Worker killed (`SIGKILL`) | Unacked messages are redelivered; a redelivered message takes over the dead worker's `p` claim; the partial work is idempotent (upsert, unique delivery key). Counters live in Redis, so `lost` stays exact | T4: worker `docker kill`, 6474/6474 alerts |
| Postgres down | Worker nacks with requeue (infra errors always requeue), nothing acked, nothing lost; API reads fail with 5xx until it returns | worker tests |
| Redis down | Worker requeues; SSE fan-out and rate limits pause; alerts already committed are unaffected; the outbox relay still publishes | worker tests |
| Crash after commit, before publish | Scheduler's outbox relay republishes rows with `published_at IS NULL` (> 5 s) | `core` tests |
| Duplicate delivery of a message | Redis claim (event), unique key + status check (delivery), `Idempotency-Key` (provider) | T6 dedup, tests |
| All LLM providers down | Fallback chain, then degraded mode: deterministic fixed-before + runbook, `aiUnavailable`; nothing on the hot path depends on it | AI tests |
| Prompt injection in telemetry | `suspicious_content` flag, banner, no AI action suggestion, high-risk actions still need a human | injection tests, `make demo-events` |
| Single instance of Postgres / RabbitMQ / Redis | Honest SPOFs of the demo; production: quorum queues on 3 nodes, Postgres primary + replica, Redis Sentinel | [spec §17](TECH_SPEC.md) |
