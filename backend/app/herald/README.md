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
| `incident_manager` | `alerts.assignee_id`: set automatically when the alert is created (`app/escalation/auto_assign.py`: named in the rule > on call > least busy member), not on ack |
| `assigned_by` / `assign_reason` | who made the current assignment (`system:auto-assign` or `person:x`) / the automatic reason (`on call for Team Payments`); both null while unassigned. Timeline `assigned` detail: `to`, `reason`, `auto` |

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
| `POST /api/approvals/{id}/decide` | done | `{approve: bool}` or `{decision, comment}`; returns `{status, result}`. Four-eyes is ours: listed approvers / Domain Roots / masters; a person never approves (and is never listed as approver of) their own request, but may reject = withdraw it. Detail `approvals[]` carry `requested_by` and `requested_by_user` (the human behind it, also for an agent) so the UI can hide the button |
| `GET /api/stream` | done | Herald frames (see below) |
| `GET /api/deliveries` | done | `latency_ms` = delivered - event received; a UI-channel delivery without a person goes to the team feed: `recipient: team:<slug>`, `recipient_kind: team`, `recipient_name: "<Team> feed"` (else `person` / `address`) |
| `GET /api/audit` | done | every signed-in person, scoped (masters: all; others: their teams' alerts / approvals / rules / sources + their own actions; empty list, never 403); `intact` is always true and `chained: false` (our log is append-only, not hash-chained); `hash` = sha256 of the row |
| `GET /api/stats` | done | Herald counters over our deliveries + `summary` = `/v1/metrics/summary`; `outstanding` ("Waiting to be sent") = deliveries `pending`/`retrying` created in the last hour, not a cumulative count |
| `/auth/*`, `/login` (SSO) | not supported | demo identity only (headers or our Bearer token) |
| `PUT` preferences | not needed | both UIs write `/v1/preferences/me` directly |

## Drafts: AI or heuristic

Every endpoint that returns a draft says who wrote it, so the UI can label it ("AI draft" vs "Generated
from rules, no AI"). Values: `ai` (a language model answered) or `heuristic` (our deterministic fallback;
it always answers, so nothing is ever empty because the LLM is off).

| Endpoint | Field |
|---|---|
| `POST /v1/sources/{id}/mapping/suggest` | `source`, `aiUnavailable` (key-name heuristic when no model) |
| `POST /v1/runbooks/parse` | `parsedBy`: `ai` / `heuristic` / `deterministic` (`source` is the runbook text in the `Runbook` schema) |
| `POST /api/runbooks/parse` | `source`: `ai` / `heuristic`, and Herald's `parsed_by`: `llm` / `parser` |
| `POST /v1/rules/from-text` | `source: ai` (no heuristic: without a model it answers 503 `ai_unavailable`) |
| `GET /v1/alerts/{id}/suggestion`, alert detail `suggestion` | `source`, `aiUnavailable` (history only: fixed-before, similar incidents, knowledge) |
| `GET /v1/alerts/{id}/resolution-note-draft` | `source`, `aiUnavailable`, `model`; heuristic = opened / ack / resolve times, steps run, approvals, escalations, comments |
| `GET /api/alerts/{id}/status-update` | `source: heuristic` (always deterministic) |
| `GET /api/alerts/{id}/postmortem` | `source: ai` when the AI timeline summary was added, else `heuristic` |

Fixed-before / similar incidents entries always carry `title` (the past alert's title, never a bare id),
`shortId` (`A-123`), `resolvedAt` and `resolutionNote`, with or without a model (older stored suggestions
are completed when read).

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

## Config endpoints

Owned by HB-CONFIG. Modules: `dsl.py` (rule translation + scoped rule loading), `platforms.py` (sources as
platforms), `explain.py` (why a rule did / didn't match; event normalization like the worker), `preview.py`
(rule preview), `runbook_view.py` (runbook / step shapes).

| Endpoint | Our service behind it |
|---|---|
| `GET /api/rules` | `rules` rows of the caller's teams -> `RuleRow` (`doc` = Herald `RuleDoc`, `source` `seed`/`api`) |
| `PUT /api/rules/{id}` | `dsl.herald_to_rule` -> `/v1/rules` validation (`_prepare`) -> `repositories.rules.insert_rule/update_rule` (versions, audit) -> `rules:changed` pub/sub (hot reload) |
| `POST /api/rules/test` | `when` -> our `match`, compiled; per-event explain over given events or the `recent:<source>` streams (newest 50) |
| `POST /api/rules/preview` | `present.build_alert`, `delivery.render` (SMS/phone/email), `recipients` + `channel_select` + effective policy, runbook steps rendered with `runbooks.lookup.render_step` |
| `GET /api/platforms`, `GET /api/sources` | `sources` (scoped) + alert counts per `source_id` + last event from the Redis stream |
| `PUT /api/platforms/{id}` | `repositories.sources.create_source` (webhook); the source key is returned once as `token`; 409 if the id exists |
| `GET /api/runbooks`, `PUT /api/runbooks/{id}`, `POST /api/runbooks/parse` | `runbooks.store.list_runbooks/create_runbook/update_runbook`, `runbooks.parsers.parse_runbook` (YAML/Markdown/link deterministic, text via AI parser -> heuristic fallback) |
| `GET /api/events/recent` | `recent:<source_id>` streams; `fired`/`closed` recomputed with the caller's current rules |
| `POST /api/events/explain` | `explain.explain_rule` for every rule in scope (batches and Alertmanager bodies split like ingest) |
| `GET /api/fields?source=` | field paths (+ `@type`, `@severity`, ... from `_meta`) of the recent events of that source |
| `GET /api/sim/samples` | `seed/fixtures/samples/*.json` + Herald's sample names (`metric_breach`, `metric_resolved`, `workflow_succeeded`, `unseen_margin_call`) |
| `POST /api/sim/send` | same envelope + preset split + confirmed RabbitMQ publish as `/v1/ingest` -> real worker |
| `GET /api/sim/mail` | Mailpit `GET {MAILPIT_API_URL}/api/v1/messages` (default `http://<SMTP_HOST>:8025`) |
| `GET /api/sim/phone`, `POST /api/sim/gateway` | mockgw `GET /admin/state`, `POST /admin/fail` (sms + phone, 24 h or 0 = back) |
| `POST /api/sim/reply` | `escalation.inbound.handle_sms` in-process (no `X-Inbound-Secret` needed) |
| `POST /ingest/{platform}` | `X-Herald-Token` = our source key (or the key in the path); JSON object, array or text (`app.pipeline.textlog`: one event per line with `key=value` pairs, timestamp, level) |

### Ids and shapes

- Platform id = source **name**. Herald's demo ids map to our seeded sources: `workflow-events` ->
  `workflow-broker`, `metrics-webhook` -> `prometheus`, `trade-events` -> `trade-gateway`, `generic-webhook`
  -> `custody`. Platform `type`: our `rabbitmq` is shown as `kafka` (the UI's "message broker"), with
  `broker: "rabbitmq"` and `topic` = the queue. A UI-connected platform keeps its display name and icon in
  `sources.config.herald`.
- Rule id = our rule uuid. A `PUT` with a non-uuid id (the UI invents `slug-xxxx` for new rules) creates a
  rule and remembers that id in `definition.herald.clientId`, so repeating the PUT updates the same rule.
- Runbook id = our runbook uuid; a non-uuid id is remembered in `attached.heraldId` the same way.
- Steps: `views.step_out` (id = 1-based `order`, our step id = `uid`); a guessed action (`suggested`) is
  also shown as `suggest`. Runbook `parsed_by`: `ai` -> `llm`, else `parser`.
- Recent event id = our `event_id` (uuid string, Herald used ints).

### Rule DSL translation (`dsl.py`)

| Herald | ours |
|---|---|
| `when` `all` / `any` | `match` `{op: and / or, conditions}` |
| `not` | pushed down with De Morgan to negated operators (`eq`<->`neq`, `in`<->`not_in`, `gt`<->`lte`, `gte`<->`lt`, `exists` flipped). `not` around `contains` / `regex` / `glob` -> 422 |
| ops `eq ne gt gte lt lte in contains regex glob exists` (+ `= != > >= < <= matches`) | `eq neq gt gte lt lte in contains regex glob exists` |
| `severity_gte X` on `@severity` | `_severity in [X and worse]` (and back) |
| `@type`, `@severity`, `@service`, `@source` ... (fields and `{{@x}}` in templates) | `_type`, `_severity`, ... (`_meta`) |
| `notify[{to, channels, hours, else}]` | `routing`: `person:` / `team:x/on-call` / targets with sms/phone -> `hard`, other teams / roles -> `soft`; channels -> `channelsBySeverity` (for the fixed severity, else all); first `hours` -> `timeWindow` |
| `to` values | `person:<id>` (user by email), `team:<slug>`, `team:<slug>/on-call` or `oncall:<slug>`, `role:<team>:<role>` |
| `present{title, body, fields, colour}` | `presentation{titleTemplate, bodyTemplate, fields, color}` |
| `group{by, window}` | `dedup.fields = by`; with `window`: `grouping{byField: by[0], windowSeconds}` |
| `severity` (null = from the event), `runbook`, `owner` | `priority`, `runbookId`, `teamId` (`person:x` is accepted -> their first team; read back as `team:<slug>`) |

Kept in `rules.definition.herald` (our compiler ignores it): `present.highlight`, `resolve_when`, the
client id, and the Herald sections as sent. A stored section is shown again only while it still
translates to what the rule holds (a `/v1` edit replaces `definition` and the derived view takes over).
Saving an existing rule leaves every section the UI did not change exactly as it was in our model, so
escalation, throttle, dedup window and per-severity channels survive a UI edit.

Known differences: on a **missing** field Herald's `not` is true for every operator, ours follows our
compiler (`neq`/`not_in` true, `lte`/`lt` false). **`resolve_when` is stored but advisory**: it drives
`closes` in `/api/events/explain` and `closed` in `/api/events/recent`, the worker does not auto-resolve on
it (Alertmanager `status: resolved` auto-resolve via the preset still works).
