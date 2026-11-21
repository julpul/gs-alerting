# Herald UI backend-for-frontend (`/api/*`)

The Herald web UIs (`backend/static/herald/`, sources in `ui/`) were written against the Herald prototype's
`/api/*` JSON. This package serves those shapes on top of our services. `/v1` (docs/openapi.yaml) stays the
public contract; nothing here changes it. Handlers resolve ids, call our repositories / runbook runner /
approvals service as the calling person, and translate the result (`views.py`). No business rules live here.

| Module | What |
|---|---|
| `identity.py` | `Actor` dependency -> `HeraldActor` (our user + `Scope`), `herald_role(scope)` |
| `routing.py` | `herald_router()`: errors become `{"detail": "..."}` |
| `ids.py` | Herald ids/statuses <-> ours |
| `views.py` | pure shape translation + `explain(match, ev)` (Herald `Why`) |
| `queries.py` | batch reads for the views (one query per kind, scope-filtered) |
| `detail.py` | Herald `AlertDetail` |
| `reports.py` | status update / postmortem text |

## Ids and statuses

| Herald | Ours |
|---|---|
| alert `id` (int), `key` | `alerts.short_id`; `key` = `A-<n>` (Herald used `AL-0017`; both are accepted in paths) |
| person `person:anna` | user whose e-mail local part is `anna` (`anna@demo`) |
| team id / owner `team:<slug>` | `teams.slug` (owner is always `team:<slug>`; we have no personal owners) |
| platform / `source` | `sources.name` |
| rule id | rule uuid (string) |
| runbook id | runbook uuid (string; seeded ones are `stable_id("runbook", key)`) |
| step `id` | the step's 1-based `order` (int); our step id (`s1`) is `uid`. Run accepts either |
| approval `id` | `approvals.short_id` (int; the number in `Reply APPROVE <n>`); also accepts the uuid |
| status `firing` | `open`, `escalated` |
| status `acked` | `acknowledged` |
| status `resolved` | `resolved` |
| delivery `sent` / `retrying` / `dead` / `pending` | `sent|delivered|acked` / `retrying` / `dead_lettered|failed` / anything else; `suppressed_*` -> `suppressed` |
| step state `done` / `failed` / `awaiting_approval` / `rejected` | `succeeded` / `failed` / `pending_approval` / `skipped` after a rejection |
| actor `person:x` / `system:<name>` / `agent:<name>` | stored actor dict `{type, id, name}` |
| `colour` | `alerts.color`, else a colour per severity |
| `incident_manager` | `alerts.assignee_id` (not set automatically on ack, unlike Herald) |

Roles (`/api/me`, a UI hint; authorization is always our scope): master -> `admin`, domain_root ->
`approver` (Herald ranks approver above editor, so the UI also lets them edit), member / informed ->
`responder`. A team's `lead` in `/api/directory` is the first Domain Root (by e-mail) of its domains: that
is who can approve in our system.

## Endpoints

| Endpoint | Status | Notes |
|---|---|---|
| `GET /api/me` | done | `mode: demo`, `provider: null`; extra `email`, `userId` |
| `GET /api/meta` | done | severities guide, our operators in Herald names, channels, actions (risk = default risk), roles, `version`, `auth`, `links` (`PUBLIC_MAILPIT_URL`, `PUBLIC_MOCKGW_URL`) |
| `GET /api/directory` | done | teams/people in the caller's scope. Team `preferences` = channels of the first escalation level per severity; person `preferences` = our `preferences.by_severity`; `has_rules` false for people |
| `GET /api/session-token` | done (demo only) | a normal /v1 access token for the current identity |
| `GET /api/alerts` | done | `owner` (`team:` or `person:` = assignee), `status` (comma list), `source`, `severity`, `limit<=500`; every status by default |
| `GET /api/alerts/{id}` | done | + `sample_event`, `runbook`, `step_state`, `why`, `timeline`, `approvals`, `suggestion`, `flags`, `escalation_level`, `escalates_at`, `uuid` |
| `POST .../ack`, `.../resolve` | done | 409 when already acked / resolved (Herald); resolve takes optional `{note}` |
| `POST .../assign` | partial | `{to: "person:x"}`; `to: null` (unassign) answers 422: our API has no unassign |
| `POST .../notes` | done | informed people may comment |
| `POST .../steps/{step}/run` | done | `{state: done}`, `{state: done|failed, result}`, `{state: awaiting_approval, approval, approver}` |
| `GET .../status-update`, `.../postmortem` | done | deterministic text; the postmortem adds resolution notes and, for resolved alerts with an LLM configured, an AI summary of the timeline (8 s cap, never fails) |
| `POST /api/approvals/{id}/decide` | done | `{approve: bool}` or `{decision, comment}`; returns `{status, result}`. Four-eyes is ours: listed approvers / Domain Roots / masters |
| `GET /api/stream` | done | Herald frames (see below) |
| `GET /api/deliveries` | done | `latency_ms` = delivered - event received |
| `GET /api/audit` | done | approver/admin only; scoped; `intact` is always true and `chained: false` (our log is append-only, not hash-chained); `hash` = sha256 of the row |
| `GET /api/stats` | done | Herald counters over our deliveries + `summary` = `/v1/metrics/summary` |
| `/auth/*`, `/login` (SSO) | not supported | demo identity only (headers or our Bearer token) |
| `PUT` preferences | not needed | both UIs write `/v1/preferences/me` directly |

## Live stream

`/api/stream` subscribes the same Redis channels as `/v1/stream` (`?owner=team:<slug>` -> that team, 403 if
not visible; otherwise every team in scope; plus the caller's user channel) and writes Herald frames:

```
id: 7
event: alert
data: {"id": 7, "owner": "team:team-payments", "kind": "alert", "alert_id": 1234, "type": "alert.acked"}
```

Kinds: `alert` (alert.created|updated|acked|escalated|resolved, suggestion.ready), `delivery`
(delivery.status), `approval` (approval.requested|decided). Ids continue from `Last-Event-ID` (no replay:
the UIs refetch on reconnect). `: keep-alive` every 15 s.

## Demo identity

`X-Herald-User: person:<id>` picks the person (default `person:master`); `X-Herald-Role` / `X-Herald-Teams`
are UI hints only. Anyone who can reach the BFF can act as anyone: this is the demo's "Viewing as" switcher,
not authentication. A Bearer JWT / PAT wins over the headers.
