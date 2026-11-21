## Herald UI integration — shared brief (read fully)

Goal: the Herald web UI (designed by our design team as the "Herald" prototype) runs on OUR backend, same origin,
so the demo is one deployment. Both UIs must work: the classic single-file UI (`backend/static/herald/index.html`,
`lab.html`, `login.html`, `flow/`) and the Atlaskit React UI (`backend/static/herald/ads/`, source in `ui/ads/`).

**Approach: backend-for-frontend.** We implement the Herald `/api/*` endpoints (plus `/ingest/{platform}` and
`/inbound/sms` aliases where the UI/lab uses them) as thin adapters over OUR services. `/v1` stays the public
contract (openapi.yaml) and is not changed for the UI except additively. No business logic in adapters — call
existing repositories/service functions (alerts repo + alert_actions, rules repo/compiler/dryrun, sources repo,
runbooks parsers/runner/approvals, delivery routing, escalation oncall/policies, ai.*, knowledge, metrics).
If something truly doesn't exist, add the smallest service function in the owning module and test it.

**Reference implementation (READ-ONLY, never edit):** `/Users/julian/Projects/work/the-herald/` —
`alertsvc/api/main.py` (every route + exact response JSON), `alertsvc/{models,store,routing,normalize,incident,
runbooks/*,rules/*}.py`, `ui/index.html` (what the classic UI reads from each response — grep `/api/`),
`ui-ads/src/api.ts` (TypeScript types = the response contract), `ui-ads/src/views/*`, `ui/lab.html`,
`docs/TEST-SCENARIOS.md`. Match field names/types exactly; extra fields are fine.

**Conventions (binding):**
- Identity: `app/herald/identity.py` (`Actor` dependency → `HeraldActor`: our user row + our `Scope`; demo headers
  `X-Herald-User: person:<email local part>`; Bearer JWT/PAT also accepted). Authorization = our scope/roles.
- Routers: put files `backend/app/api/routers/herald_<area>.py` with `router = herald_router()` from
  `app/herald/routing.py` (errors become `{"detail": ...}` which the UI reads). Paths are the Herald ones (`/api/...`).
- Ids: alert id = our `short_id` (int); person id = email local part (`anna`), ref `person:anna`; team id = team
  slug; platform id = source **name** (unique); rule id = our rule uuid as string; runbook id = our runbook uuid as
  string (seeded ones have stable ids); approval id = `approvals.short_id` (int); step id = index/our step id —
  pick one consistently and document it in `app/herald/README.md`.
- Status mapping: Herald `firing|acked|resolved` ↔ ours `open|escalated → firing`, `acknowledged → acked`, `resolved`.
  Herald severity strings = ours. Colour = our `color` (fallback by severity).
- Time: ISO strings like Herald.
- Shape translation helpers live in `app/herald/views.py` (owned by HB-CORE) and `app/herald/dsl.py` (owned by
  HB-CONFIG). Other owners import them; request additions via your report.
- Tests: `backend/tests/herald/` with the root conftest fixtures (`client`, `seeded`, …; seed profile is `core`).
  For each endpoint at least: shape test (keys/types the UI reads) + one behaviour test. Whole suite must stay green.

**Ownership:**
- HB-CORE: `app/herald/{views,ids}.py`, routers `herald_me.py` (`/api/me`, `/api/meta`, `/api/directory`,
  `/api/session-token`), `herald_alerts.py` (`/api/alerts*` incl. ack/resolve/assign/notes/steps run/
  status-update/postmortem), `herald_approvals.py`, `herald_stream.py` (`/api/stream` SSE in Herald format,
  fed by our Redis SSE channels), `herald_feed.py` (`/api/deliveries`, `/api/audit`, `/api/stats`), tests
  `tests/herald/test_core_*.py`.
- HB-CONFIG: `app/herald/{dsl,platforms}.py`, routers `herald_rules.py` (`/api/rules*` incl. preview/test),
  `herald_platforms.py` (`/api/platforms*`, `/api/sources`), `herald_runbooks.py`, `herald_events.py`
  (`/api/events/recent`, `/api/events/explain`, `/api/fields`), `herald_sim.py` (`/api/sim/*` → our mockgw /
  Mailpit / ingest), `herald_ingest.py` (`POST /ingest/{platform}` alias with `X-Herald-Token` = our source key),
  tests `tests/herald/test_config_*.py`.
- HUI: `ui/**`, `backend/static/herald/**`, `app/api/mounts/herald_ui.py` (serve the static UI at `/`), new React
  screens, Docker/compose/Railway wiring for the UI.
