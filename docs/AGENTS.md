# Subagent configuration

How this repo was built with Claude Code subagents. Definitions live in `.claude/agents/*.md` (frontmatter = model + reasoning effort; body = system prompt).

| Agent | Model | Effort | Role | Writes code? |
| --- | --- | --- | --- | --- |
| `orchestrator` | Claude Opus 5.5 (`opus`) | high | Plans phases, writes task briefs, spawns implementers in parallel, reviews diffs, merges, runs gates, keeps `docs/PROGRESS.md` | Integration glue only |
| `implementer` | Claude Sonnet 5.5 (`sonnet`) | high | Implements exactly one plan task inside its owned files, with tests, in an isolated git worktree; commits to its branch | Yes |
| `qa-verifier` | Claude Sonnet 5.5 (`sonnet`) | high | Independent phase gate: lint, tests + coverage, compose up, smoke checks against the OpenAPI contract | No (reports only) |

In practice the main Claude Code session runs on Opus 5.5 with high effort and acts as the orchestrator (same prompt as `.claude/agents/orchestrator.md`); it spawns `implementer` and `qa-verifier` agents with `model: sonnet`, `effort: high`.

## Why this shape
- **One task = one owner = one worktree.** The plan assigns each task an exclusive set of files, so parallel agents never edit the same file. Shared registries are avoided by design: routers and seed loaders are auto-discovered, migrations use per-task number ranges.
- **Binding interfaces.** `docs/IMPLEMENTATION_PLAN.md` §3 fixes the function signatures between modules, so e.g. the ingest worker and the rule engine are built at the same time against the same contract. Only the orchestrator may change them.
- **Contract-first API.** `docs/openapi.yaml` is shared with the UI team; implementers must match it exactly.
- **Independent verification.** The verifier didn't write the code and checks behaviour end to end, not just unit tests.
- **Parallel-safe tests.** Every pytest session creates its own Postgres database and a random RabbitMQ/Redis prefix, so several agents can run suites against the same local infra simultaneously.

## Phase flow
```
P0 orchestrator ─► P1 FOUND ─► P2 {INGEST, RULES, DELIVERY, ALERTSAPI} ─► gate
                ─► P3 {ESCAL, RUNBOOK, AI, LOAD, ORG} ─► gate ─► P4 {MCP, CONNECT} ─► gate ─► P5 deploy/perf/docs
```

## Task brief template (orchestrator → implementer)
```
Task: <ID> — <title>
Worktree branch: task/<ID>
Read: CLAUDE.md; docs/IMPLEMENTATION_PLAN.md §2 <ID>, §3 interfaces <list>, §4 migration range <NNN–NNN>; docs/TECH_SPEC.md §<sections>; openapi paths <list>
State of main: <what is merged and usable>
Owns: <files>
Builds: <bullets>
Consumes (code against these signatures, fake in tests if not merged yet): <list>
Done when: <checklist>
Report: format from .claude/agents/implementer.md
```

## Security & privacy while using AI (for the submission)
- Agents run locally in Claude Code with a permission mode; no production data exists — all events/users are synthetic ("Vistula Capital", see `docs/DEMO_ORG.md`).
- Secrets live only in local `.env` / GitHub Actions secrets / Railway variables; agents are instructed never to print or commit them (`.gitignore` covers `.env*`).
- Every agent change goes through tests + an independent verifier + orchestrator review before merge.
