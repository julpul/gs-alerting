# PROGRESS — live tracker

Edited only by the orchestrator. Status: `todo` · `in_progress` · `review` · `done` · `blocked` · `cut`.
Plan and task definitions: `docs/IMPLEMENTATION_PLAN.md`.

## Phase status

| Phase | Status | Gate result | Notes |
| --- | --- | --- | --- |
| P0 repo/docs/agents/CI | done | — | repo julpul/gs-alerting (private), CI+CD workflows |
| P1 foundation | done | PASS (69 tests, 97 % cov, compose up, login ok) | merged 29655d7 |
| P2 core pipeline | done | **PASS** (QA: demo events → 5 rules, Mailpit + mockgw deliveries, SSE, dedup ×501 → 1 alert, SMS outage → DLQ + email fallback, isolation, coverage 97.6 %) | 3 minor findings fixed by orchestrator |
| P3 video features + measurement | in_progress | — | ESCAL, RUNBOOK, AI, ORG-B in parallel; LOAD + sim A merged |
| P4 agents + sources | in_progress | — | CONNECT + MCP in parallel |
| P5 ship | todo | — | |

## Tasks

| ID | Status | Agent / branch | Started | Done | Coverage | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | done | orchestrator | 2026-11-21 | 2026-11-21 | | |
| P1-FOUND | done | implementer / task/P1-FOUND | 2026-11-21 | 2026-11-21 | 97 % | deviations: role_assignments.scope_id NOT NULL (ORG_ID), own ORJSONResponse, personal_tokens.revoked_at |
| P2-INGEST | done | implementer / task/P2-INGEST | 2026-11-21 | 2026-11-21 | ≥ 90 % | two-phase idempotency key; infra errors always requeue; `_source` = producer else source name |
| P2-RULES | done | implementer / task/P2-RULES | 2026-11-21 | 2026-11-21 | ≥ 90 % | 5 seeded rules + custody source; regex guard = pattern cap + nested-quantifier reject + input truncation |
| P2-DELIVERY | done | implementer / task/P2-DELIVERY | 2026-11-21 | 2026-11-21 | ≥ 90 % | DLQ after 3 attempts (5 s, 30 s); empty hard list → domain roots; digest sender not built yet |
| P2-ALERTSAPI | done | implementer / task/P2-ALERTSAPI | 2026-11-21 | 2026-11-21 | 91–100 % | contract additions (shortId, escalatesAt, MetricsSummary fields); ack/resolve idempotent; informed = read+comment |
| P3-ESCAL | done | implementer / task/P3-ESCAL | 2026-11-21 | 2026-11-21 | 96–100 % | scheduler (escalate/renotify/relay/expiry/shifts/digest) SKIP LOCKED-safe; inbound SMS ACK/RESOLVE/APPROVE; `digested` status |
| P3-RUNBOOK | done | implementer / task/P3-RUNBOOK | 2026-11-21 | 2026-11-21 | 85–100 % | 6 formats (YAML/MD/text/link/checklist/JSON); guards advisory; approver SMS sent directly via adapters |
| P3-AI | done | implementer / task/P3-AI | 2026-11-21 | 2026-11-21 | 91–100 % | LiteLLM router + fallback, redaction, untrusted_event, L1+L2 hybrid retrieval (fastembed + pgvector + FTS), degraded mode; context.py (GitHub/Datadog) skipped (wave 3) |
| P3-LOAD | review | implementer / task/P3-LOAD | 2026-11-21 | merged | 25 tests | merged; not yet run against real backend (after P2) |
| P3-ORG | done | implementer / task/P3-ORG-sim + task/P3-ORG-B | 2026-11-21 | 2026-11-21 | sim 99.6 %, seed 97 % | part A merged (simulator, org_full.yaml 25 users, 40-incident history, 6 knowledge docs); part B = seed loaders + 5th rule `security-typed-alerts` after P2 |
| P4-MCP | in_progress | implementer / task/P4-MCP | 2026-11-21 | | | migrations 085–089 |
| P4-CONNECT | in_progress | implementer / task/P4-CONNECT | 2026-11-21 | | | started early (independent of P3) |
| P5-DEPLOY | todo | orchestrator | | | | |
| P5-PERF | todo | | | | | |
| P5-DOCS | todo | | | | | |

## Measured results (filled from loadtest/results)

| Test | Result | Target | Date |
| --- | --- | --- | --- |
| T1 latency ingest→UI p50/p99 | | < 50 ms / < 300 ms | |
| T1 latency ingest→SMS/email p99 | | < 1 s | |
| T2 max sustained ev/s (4 workers) | | ≥ 1000 | |
| T4 lost alerts in chaos | | 0 | |
| T5 throughput 1/2/4 workers | | linear-ish | |
| T6 dedup 500 → | | 1 alert, 1 hard notification | |

## Decision log

| When | Decision | Why |
| --- | --- | --- |
| P0 | Agents work in isolated git worktrees, orchestrator merges | Parallel tasks without stepping on each other's half-written files |
| P0 | Routers/seed loaders auto-discovered, migrations by number range | No shared registry file ⇒ conflict-free merges |
| P0 | Role `all` / comma-separated roles in one process | Cheap Railway deploy (2 app services) while compose still scales roles independently |
| P0 | P3-LOAD and simulator started in parallel with P1 | Independent of backend code (HTTP contract + fixed demo keys) — saves ~1.5 h |
| P3 | `SEED_PROFILE=core` in tests (7 users), `full` (25 users + history + knowledge) for demo | Older tests assert exact small-org membership; full org seeded in its own test DB |
| P0 | Tests use per-session DB + MQ/Redis prefix | Parallel agents share one local infra safely |

## Open issues / requests between owners

- ~~Metrics HTTP server 9101~~ done.
- openapi additions from P3-ESCAL to document: `from`/`to` on GET /v1/schedules, `domainId` on effective policy, `Shift.userName` (P5-DOCS).
- `/v1/inbound/sms` unauthenticated (contract `security: []`) → add shared-secret header from mockgw (P5).
- ~~Load-test Makefile include + monitoring compose~~ done.
- **GitHub Actions does not start on this account (even a minimal workflow → startup_failure).** Account-level (billing/minutes for private repos). Needs user: check billing or make repo public.
- P3-ORG part B: org loader from `simulator/org_data/org_full.yaml`, `90_history`, knowledge docs, 5th rule security-typed-alerts, `demo-key-custody` source.
