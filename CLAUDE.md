# CLAUDE.md — gs-alerting

Flexible Alerting & Notification Service (Goldman Sachs Hackathon 2026).

## Source of truth (read in this order)
0. `docs/BRIEF.md` — the original hackathon brief incl. Appendix A (sample events) and B (sample runbooks).
1. `docs/TECH_SPEC.md` — architecture, behaviour, data model. Wins over everything else.
2. `docs/openapi.yaml` — REST/SSE wire contract. **Change the contract first, then code.** Never break it silently.
3. `docs/IMPLEMENTATION_PLAN.md` — phases, tasks, file ownership, interfaces between modules.
4. `docs/PROGRESS.md` — live status tracker. Only the orchestrator edits it.

## Layout
```
backend/      Python 3.12 FastAPI service, one image, many roles (app.main --role ...)
mockgw/       Fake SMS/phone/workflow/ticket gateway with chaos toggle
simulator/    "Vistula Capital" — fictional client org that emits realistic traffic into us
infra/        docker-compose, prometheus, grafana, railway notes
loadtest/     k6 scripts, chaos script, report.py, results/
docs/         spec, contract, plan, progress, manual steps, agents
.claude/agents/  orchestrator + implementer + qa-verifier definitions
```

## Commands
```bash
make infra-up        # postgres, redis, rabbitmq, mailpit, mockgw only (for tests / local dev)
make up              # full stack (compose) + migrate + seed
make test            # pytest with coverage (needs infra-up)
make lint            # ruff check + ruff format --check
make demo-events     # Appendix A samples + injection sample
make t1 | t2 | t4 | t6 | report   # load tests
cd backend && uv run pytest tests/path -x -q   # one area
```

## Conventions (non-negotiable)
- Python 3.12, `uv`, async everywhere (asyncpg, aio-pika, redis.asyncio, httpx). No ORM: raw SQL in `app/repositories/` or the module that owns the table.
- Wire format camelCase via pydantic aliases; DB snake_case; timestamps UTC ISO-8601 with ms.
- Every DB query that returns user-visible data takes a `Scope` and filters by it. No unscoped reads outside seed/admin/workers.
- Hot path (ingest → worker → delivery) **never** calls an LLM and never does per-event Postgres writes (only alerts/deliveries/outbox).
- Event content is untrusted: templates are `{{path}}` substitution only, no Jinja/eval; content sent to LLMs is redacted and wrapped in `<untrusted_event>`.
- Migrations: plain `.sql` files in `backend/migrations/`, applied in filename order at startup. Never edit a migration that is already merged — add a new one in your assigned number range (see plan §Migrations).
- Routers are auto-discovered: put a module with a module-level `router = APIRouter(...)` in `backend/app/api/routers/`. Do not edit `app/api/app.py` to register routers.
- Seed loaders are auto-discovered from `backend/seed/loaders/NN_name.py` (function `async def load(conn, fixtures)`), fixtures are YAML in `backend/seed/fixtures/`.
- Tests: pytest + pytest-asyncio, real Postgres/Redis/RabbitMQ from `make infra-up`. Each test session uses its own database and key/queue prefix (handled by `tests/conftest.py`), so parallel agents don't collide. LLM is stubbed in tests only (`tests/fakes/llm.py`); product code always uses the real LLM.
- Secrets only in `.env` (git-ignored). Never commit keys. Never print keys in logs.
- Keep files focused (< ~400 lines). Type hints on public functions. No dead code, no commented-out code.
