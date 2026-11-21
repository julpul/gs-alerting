# PROGRESS — live tracker

Edited only by the orchestrator. Status: `todo` · `in_progress` · `review` · `done` · `blocked` · `cut`.
Plan and task definitions: `docs/IMPLEMENTATION_PLAN.md`.

## Phase status

| Phase | Status | Gate result | Notes |
| --- | --- | --- | --- |
| P0 repo/docs/agents/CI | done | — | repo julpul/gs-alerting (private), CI+CD workflows |
| P1 foundation | done | PASS (69 tests, 97 % cov, compose up, login ok) | merged 29655d7 |
| P2 core pipeline | review | 485 tests green locally; QA gate running | all 4 merged |
| P3 video features + measurement | todo | — | |
| P4 agents + sources | todo | — | |
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
| P3-ESCAL | todo | | | | | |
| P3-RUNBOOK | todo | | | | | |
| P3-AI | todo | | | | | |
| P3-LOAD | review | implementer / task/P3-LOAD | 2026-11-21 | merged | 25 tests | merged; not yet run against real backend (after P2) |
| P3-ORG | in_progress | implementer / task/P3-ORG-sim | 2026-11-21 | | sim 99.6 % | part A merged (simulator, org_full.yaml 25 users, 40-incident history, 6 knowledge docs); part B = seed loaders + 5th rule `security-typed-alerts` after P2 |
| P4-MCP | todo | | | | | |
| P4-CONNECT | todo | | | | | |
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
| P0 | Tests use per-session DB + MQ/Redis prefix | Parallel agents share one local infra safely |

## Open issues / requests between owners

- Metrics HTTP server (port 9101) for non-api roles must be started once in `app/main.py` (orchestrator wiring after P2).
- ~~Load-test Makefile include + monitoring compose~~ done.
- **GitHub Actions does not start on this account (even a minimal workflow → startup_failure).** Account-level (billing/minutes for private repos). Needs user: check billing or make repo public.
- P3-ORG part B: org loader from `simulator/org_data/org_full.yaml`, `90_history`, knowledge docs, 5th rule security-typed-alerts, `demo-key-custody` source.
