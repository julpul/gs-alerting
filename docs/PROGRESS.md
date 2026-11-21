# PROGRESS — live tracker

Edited only by the orchestrator. Status: `todo` · `in_progress` · `review` · `done` · `blocked` · `cut`.
Plan and task definitions: `docs/IMPLEMENTATION_PLAN.md`.

## Phase status

| Phase | Status | Gate result | Notes |
| --- | --- | --- | --- |
| P0 repo/docs/agents/CI | done | — | repo julpul/gs-alerting (private), CI+CD workflows |
| P1 foundation | done | PASS (69 tests, 97 % cov, compose up, login ok) | merged 29655d7 |
| P2 core pipeline | in_progress | — | 4 agents in parallel |
| P3 video features + measurement | todo | — | |
| P4 agents + sources | todo | — | |
| P5 ship | todo | — | |

## Tasks

| ID | Status | Agent / branch | Started | Done | Coverage | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | done | orchestrator | 2026-11-21 | 2026-11-21 | | |
| P1-FOUND | done | implementer / task/P1-FOUND | 2026-11-21 | 2026-11-21 | 97 % | deviations: role_assignments.scope_id NOT NULL (ORG_ID), own ORJSONResponse, personal_tokens.revoked_at |
| P2-INGEST | in_progress | implementer / task/P2-INGEST | 2026-11-21 | | | |
| P2-RULES | in_progress | implementer / task/P2-RULES | 2026-11-21 | | | |
| P2-DELIVERY | in_progress | implementer / task/P2-DELIVERY | 2026-11-21 | | | |
| P2-ALERTSAPI | in_progress | implementer / task/P2-ALERTSAPI | 2026-11-21 | | | |
| P3-ESCAL | todo | | | | | |
| P3-RUNBOOK | todo | | | | | |
| P3-AI | todo | | | | | |
| P3-LOAD | in_progress | implementer / task/P3-LOAD | 2026-11-21 | | | started early (no backend dependency) |
| P3-ORG | in_progress | implementer / task/P3-ORG-sim | 2026-11-21 | | | part A (simulator + org data drafts) started early; part B = seed loaders after P2 |
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
- Load-test Makefile include + monitoring compose (after P3-LOAD lands).
