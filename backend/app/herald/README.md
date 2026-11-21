# Herald UI backend-for-frontend

`/api/*` (+ `/ingest/{platform}`) in the shapes the Herald UI reads, as thin adapters over our services.
Identity: `identity.py` (`Actor`), routers: `app/api/routers/herald_*.py` built with `routing.herald_router()`.

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
| `POST /ingest/{platform}` | `X-Herald-Token` = our source key (or the key in the path); JSON object, array or a text line |

### Ids and shapes

- Platform id = source **name**. Herald's demo ids map to our seeded sources: `workflow-events` ->
  `workflow-broker`, `metrics-webhook` -> `prometheus`, `trade-events` -> `trade-gateway`, `generic-webhook`
  -> `custody`. Platform `type`: our `rabbitmq` is shown as `kafka` (the UI's "message broker"), with
  `broker: "rabbitmq"` and `topic` = the queue. A UI-connected platform keeps its display name and icon in
  `sources.config.herald`.
- Rule id = our rule uuid. A `PUT` with a non-uuid id (the UI invents `slug-xxxx` for new rules) creates a
  rule and remembers that id in `definition.herald.clientId`, so repeating the PUT updates the same rule.
- Runbook id = our runbook uuid; a non-uuid id is remembered in `attached.heraldId` the same way.
- **Step id = 1-based position** of the step in its runbook (Herald ints); our own step id is `key`.
  Step `kind`: our `text` -> `manual`; a guessed action (`suggested`) is shown as `suggest`, `action: null`.
  `guard` -> `condition`, `parameters` -> `params`. Runbook `parsed_by`: `ai` -> `llm`, else `parser`.
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
| `severity` (null = from the event), `runbook`, `owner` | `priority`, `runbookId`, `teamId` (`person:x` -> their first team) |

Kept in `rules.definition.herald` (our compiler ignores it): `present.highlight`, `resolve_when`, a person
owner, the client id, and the Herald sections as sent. A stored section is shown again only while it still
translates to what the rule holds (a `/v1` edit replaces `definition` and the derived view takes over).
Saving an existing rule leaves every section the UI did not change exactly as it was in our model, so
escalation, throttle, dedup window and per-severity channels survive a UI edit.

Known differences: on a **missing** field Herald's `not` is true for every operator, ours follows our
compiler (`neq`/`not_in` true, `lte`/`lt` false). **`resolve_when` is stored but advisory**: it drives
`closes` in `/api/events/explain` and `closed` in `/api/events/recent`, the worker does not auto-resolve on
it (Alertmanager `status: resolved` auto-resolve via the preset still works).
