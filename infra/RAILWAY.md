# Railway deployment

Project `gs-alerting` · environment `production` (workspace: Julian Pulawski's Projects).

| Service | Source | Role / notes | Public URL |
| --- | --- | --- | --- |
| `api` | repo `backend/` (Dockerfile) | `APP_ROLE=api`, runs migrations on start | https://api-production-d033bedd.up.railway.app |
| `workers` | repo `backend/` (Dockerfile) | `APP_ROLE=worker,delivery,scheduler,ai,connector`, `SKIP_MIGRATIONS=true` | — |
| `mockgw` | repo `mockgw/` | fake SMS/phone/workflow gateway, port 8090 | https://mockgw-production.up.railway.app |
| `simulator` | repo `simulator/` | Vistula Capital traffic, port 8095 | https://simulator-production-ae5fd2bb.up.railway.app |
| `mailpit` | image `axllent/mailpit` | inbox UI on 8025, SMTP 1025 (private) | https://mailpit-production-69850428.up.railway.app |
| `postgres` | image `pgvector/pgvector:pg16` + volume | private only | — |
| `Redis` | Railway Redis template + volume | private only | — |
| `rabbitmq` | image `rabbitmq:3.13-management` + volume | private only | — |

Variables reference infra via Railway references (`${{postgres.POSTGRES_PASSWORD}}`, `${{Redis.REDIS_URL}}`, `${{rabbitmq.RABBITMQ_DEFAULT_PASS}}`) and private DNS (`*.railway.internal`). `JWT_SECRET` / `APP_SECRET` were generated randomly at setup. LLM keys: see `docs/MANUAL_STEPS.md` §1.

**CD:** `.github/workflows/cd.yml` — push to `prod` ⇒ CI ⇒ `railway up <path> --path-as-root --service <name>` for api, workers, mockgw, simulator ⇒ smoke `GET $RAILWAY_API_URL/readyz`. Needs repo secret `RAILWAY_TOKEN` (project token, see MANUAL_STEPS §4).

Scale workers: Railway → `workers` → Settings → Replicas (stateless; dedup/idempotency in Redis/Postgres make it safe).
