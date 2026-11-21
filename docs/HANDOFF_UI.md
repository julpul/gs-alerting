# UI Handoff — Flexible Alerting & Notification Service

Goldman Sachs Hackathon 2026. Shared spec between the backend (`backend/`, Python + FastAPI) and the UI (`ui/`).
The API contract is `openapi.yaml` next to this file. **Both sides code against it. If something must change, change `openapi.yaml` first and tell the other side.**

Full technical spec: `../TECH_SPEC.md` (architecture, behaviour, data model). This file is the UI-relevant subset.

**Designer: you own the look and feel.** This document defines *what* each screen must do, which data it shows and which states it has (section 2b). Layout, visual language, components and motion are yours. Two constraints only: the hard rules in section 2, and that every screen in wave 1–2 exists for the video.

---

## 1. What we are building (one paragraph)

One place where **people and AI agents** report problems, the problem reaches the right person on the right channel, and they fix it together — while AI never acts without permissions and evidence. Events arrive in any schema, team rules decide who gets notified (hard = phone/SMS + must acknowledge, soft = email/UI for managers), duplicates collapse into one alert with a counter, unacknowledged alerts escalate up the org pyramid, and every alert shows the runbook and an AI-suggested fix.

Three differentiators the UI must make visible:

1. **Pyramid for humans and agents** — permissions flow down (Master → Domain Root → Member), escalation flows up. AI agents can raise alerts and ask humans for approval through the same routing.
2. **Safe hands** — every AI output is a *suggestion* with Accept/Reject; risky actions need approval; events with suspicious content (prompt injection in logs) are flagged; actions earn a trust level.
3. **Team memory** — resolved incidents become knowledge; the next similar alert shows "this was fixed before, here's how".

## 2. Hard rules for the UI

- **AI is always labelled.** Anything produced by AI (suggested fix, rule draft, schema mapping, runbook parse, resolution note) is shown as a draft with a visible "AI suggestion" label and explicit **Accept / Edit / Reject**. Never auto-apply.
- **Risky = visibly risky.** Runbook actions with `risk: high` or `approval: required` get a distinct style and an approval state (pending / approved by X / rejected).
- **Untrusted content warning.** If `alert.flags` contains `suspicious_content`, show a warning banner: "This event contains text that looks like instructions. AI actions are disabled for it."
- **Scope is always visible.** Header shows the current domain/team switcher. Users only see their scope; never render data the API didn't return.
- **Usable by non-engineers.** Plain language, no raw JSON by default (raw payload behind a "Show raw event" toggle). Empty states explain the next step.
- **Severity colours are consistent everywhere:** `critical` red, `error` orange, `warning` yellow, `info` blue/grey. Rules may override colour via `presentation.color`.
- **Real-time without refresh.** Feed and detail views update from the SSE stream (`GET /v1/stream`), not polling.

## 2b. States every screen needs designed

| State | Where | What to show |
| --- | --- | --- |
| Loading | all lists/details | skeletons, not spinners on the whole page |
| Empty | feed, approvals, rules, sources, knowledge | one sentence + the next action ("No alerts. Connect a source →") |
| Error / backend down | all | inline message + retry; feed keeps last data and shows "reconnecting…" |
| Live update | feed, detail, approvals badge | subtle highlight on changed row/counter; toast for escalations and approval requests |
| AI analysing | alert detail suggestion panel | "Analysing… (usually a few seconds)" skeleton |
| AI draft | suggestion, rule draft, mapping draft, runbook parse, resolution note | clear "AI suggestion" label + Accept / Edit / Reject |
| Suspicious content | alert detail + feed row icon | warning banner, AI actions disabled |
| Pending approval | runbook step, approvals inbox | who must approve, expiry countdown |
| Escalation countdown | feed row + detail | "Escalates to Domain Root in 3:12" |
| Inherited value | escalation settings | value shown with origin chip ("from Org", "from Domain: Payments"), "Override" / "Reset to inherited" |
| Permission-limited | any admin control | hidden for roles that can't use it (not just disabled) |

## 3. Tech

- React + TypeScript + Vite. Suggested: TanStack Query, React Router, React Flow (rule builder), Monaco or CodeMirror (YAML editor), Tailwind or any component lib the designer prefers.
- Types: generate from `openapi.yaml` (`npx openapi-typescript openapi.yaml -o src/api/schema.d.ts`).
- **Mock-first.** Build against MSW (Mock Service Worker) handlers using fixtures from section 8 until the backend is up. Switch with `VITE_USE_MOCKS=true|false`.
- Config: `VITE_API_URL` (default `http://localhost:8080`).
- Auth: `POST /v1/auth/login` returns `accessToken` (JWT). Send `Authorization: Bearer <token>`. SSE: `GET /v1/stream?access_token=<token>` (EventSource can't set headers).
- Must run with one command in the main `docker compose up` (serve the built app from an nginx container on port 3000).

## 4. Roles (what each role sees)

| Role | Sees | Can do |
| --- | --- | --- |
| Master | Everything in the org | Create domains, assign Domain Roots, global settings, all below |
| Domain Root | Their domain + cross-domain teams they belong to | Manage sources, teams, rules, schedules, roles below them; escalation level 2 |
| Member (on-call) | Their teams' alerts | Ack/resolve/assign, run runbook steps, approve if allowed, propose rules |
| Informed (soft) | Alerts where they are on the soft list | Read, comment |
| Agent (service account) | Nothing in UI | Shown as an actor: "agent pr-reviewer on behalf of Anna" |

`GET /v1/auth/me` returns roles and scopes. Hide (don't just disable) things the user can't do.

## 5. Screens, in build priority

Wave 1 = must exist for the first end-to-end demo. Wave 2 = everything in the video. Wave 3 = if time allows.

### Wave 1

**5.1 Login** — email + password (demo users seeded, see section 8).

**5.2 Live alert feed** (`/alerts`) — the home screen.
- List/table: severity chip, title (already rendered by backend from the rule's template), source, team, status (`open`, `acknowledged`, `escalated`, `resolved`), **dedup counter** ("×500", updates live), last seen (relative time), assignee, escalation level.
- Filters: status, severity, team, domain, source, text search. Default: my teams, not resolved.
- Row actions: Acknowledge, Resolve, Assign.
- New alerts animate in from SSE `alert.created`; counters update from `alert.updated`.

**5.3 Alert detail** (`/alerts/:id`)
- Header: title, severity, status, counter, first/last seen, team, rule name, escalation level + "escalates to X in 3:12" countdown when not acknowledged.
- Body: fields chosen by the rule (`presentation.fields`) as label/value pairs; "Show raw event" toggle.
- **Runbook panel:** ordered steps; text steps as checklist; action steps with Run button; approval state; result of each run.
- **AI suggestion panel** (arrives later via SSE `suggestion.ready` — show skeleton "Analysing…"): short diagnosis, suggested next step, similar past incidents (link + how they were fixed), sources (logs/commits links), confidence. Accept / Reject.
- **Deliveries:** table of recipient × channel × status (`pending`, `sent`, `failed`, `retrying`, `delivered`, `acked`), split **Hard** vs **Soft**.
- **Timeline:** every event (created, notified, escalated, acked by, action run by, approved by, AI suggestion, comment, resolved). Actor shows human or "agent X on behalf of Y".
- Comment box. Resolve dialog asks "How did you fix it?" (prefilled with AI draft note) → saved to knowledge.

**5.4 Mock gateway viewer** — a simple page (or link to the mock gateway page at `http://localhost:8090`) listing SMS/phone "deliveries" so the video can show them. Email is shown in Mailpit (`http://localhost:8025`).

### Wave 2

**5.5 Approvals inbox** (`/approvals`) — pending approvals for me: runbook risky actions and **agent requests** (`origin: agent_request`). Show who/what asks ("Claude Code on behalf of Anna"), exact action + parameters (readable, raw JSON behind toggle), risk, reason, related alert, expiry countdown. Approve / Reject with comment. Badge with count in the nav. Note: approvers can also reply `APPROVE 12` by SMS — the inbox must update live when that happens (`approval.decided`).

**5.6 Rules** (`/rules`, `/rules/:id`)
- List: name, team, enabled, last fired, fires/24h, **actionability** (% of alerts that someone acted on), owner.
- Editor with three tabs that edit the same rule JSON:
  1. **Describe** — text box "What should alert whom?" → `POST /v1/rules/from-text` → shows the generated rule as an AI draft.
  2. **Visual** — React Flow: `Source/Schema` → `Conditions` (AND/OR groups) → `Routing` (Hard list, Soft list, channels per severity) → `Presentation` (title/body templates with `{{path}}` placeholders, fields, colour) → `Runbook`. Plus side settings: dedup fields + window, grouping key + window, throttle, time window, escalation timeouts.
  3. **YAML** — code editor, validated on save.
- **Dry-run** button: `POST /v1/rules/dry-run` → "Would have matched 37 events in the last hour" + sample matches + who would have been notified.
- Save creates a new version; version history list.

**5.7 Sources & onboarding wizard** (`/sources/new`) — the "time to first alert" flow; keep it to 4 steps and show a stopwatch.
1. Name + team + type (webhook, RabbitMQ, preset: Alertmanager / Sentry / Datadog / CloudEvents).
2. Show endpoint URL + key + copy-ready snippets (curl, Python, Java).
3. Paste a sample event (or "wait for first event") → `POST /v1/sources/{id}/mapping/suggest` → AI mapping draft (source field → common field: severity, service, environment, timestamp, title). Accept / edit.
4. "Waiting for your first event…" live → success screen with link to the alert + suggestion to create a rule.

Source detail page also shows **Unrouted events** (events that matched no rule, from `GET /v1/sources/{id}/sample-events`) with a "Create rule from this event" button — nothing disappears silently.

**5.8 On-call schedule** (`/teams/:id/schedule`)
- Week calendar for one team: rows = team members, columns = days (with hours on hover/zoom). Shifts are coloured bars; overrides drawn on top in a distinct style.
- Create a shift by dragging on a row (`POST /v1/schedules`, `kind: manual`).
- "Create rotation" dialog: members in handover order (drag to reorder), shift length (daily / weekly / custom hours), handover time, timezone (`POST /v1/rotations`); backend generates shifts.
- "Add override" ("Piotr covers Anna Tue 18:00 – Wed 09:00") → `kind: override`.
- Header card: **On call now** + **Next** + escalation chain (Level 1 on-call → Level 2 Domain Root → Level 3 Master) from `GET /v1/teams/{id}/oncall`. Same "On call now" chip appears on alert detail.
- Permissions: a member edits only their own shifts and can request a swap; Domain Root edits the whole team; Master edits all.

**5.9 Escalation settings** (`/settings/escalation`, visible to Master and Domain Roots; rule-level overrides also inside the rule editor)
- Scope picker: Organization → Domain → Team (rule level lives in the rule editor).
- One card per severity (critical, error, warning, info) showing the escalation ladder as a vertical timeline: Level 1 (who + channels) → "after 5 min without ACK" → Level 2 → … plus "repeat every 2 min until ACK" and soft recipients/channels/digest.
- Every value shows its **origin chip** from `GET /v1/escalation-policies/effective` (`origin`), with "Override here" and "Reset to inherited".
- Default org values to display on first load (from backend seed):

| Severity | Level 1 | → Level 2 | → Level 3 | Repeat | Soft |
| --- | --- | --- | --- | --- | --- |
| critical | on-call: phone + SMS | 5 min → Domain Root: phone + SMS | 5 min → Master | every 2 min | email + UI now |
| error | on-call: SMS | 15 min → Domain Root: SMS | 15 min → Master | every 10 min | email + UI now |
| warning | on-call: email | 60 min → Domain Root: email | — | — | hourly digest |
| info | UI only | — | — | — | — |

**5.10 My notification preferences** (`/settings/notifications`) — channels per severity (toggle grid: rows severities, columns UI/email/SMS/phone), working hours + timezone, phone number.

**5.10b AI copilot drawer** (available on every screen, real LLM — not mocked)
- Side drawer / command bar ("Ask the copilot"). Sends `POST /v1/agent/chat` with the conversation and `context` (current alert/team).
- Streams SSE: `token` (text), `tool_call` / `tool_result` (show as compact chips: "Searched past incidents", "Ran dry-run: 37 matches"), `draft` (render a card with Open / Accept / Reject linking to the rule, mapping, runbook or approval).
- Example prompts as starter chips: "Why is this alerting?", "What fixed this last time?", "Create a rule: critical from payments-api → Anna by SMS 8–20".
- Copilot can only create drafts and approval requests — never applies changes; make that obvious in the UI.

### Wave 3

**5.11 Metrics** (`/metrics`) — **needed for the video, build in wave 2 if possible.** Big numbers: events/s, p50/p99 ingest→delivery, queue depth, ingested vs delivered vs dead-lettered, **lost = 0** (green, large). Poll `GET /v1/metrics/summary` every 1 s, or embed Grafana (`http://localhost:3001`).
**5.12 Org admin** (`/admin`) — domains, teams (members can come from different domains; a team can belong to several domains), users, role assignment (can't grant above own role).
**5.13 Knowledge** (`/knowledge`) — upload Markdown/PDF, add link, connector status (Jira, Confluence: "coming soon"), list of documents + resolution notes.
**5.14 Integrations** (`/settings/integrations`) — Datadog, GitHub (read-only tokens), LLM provider: default / own API key / own endpoint (keys are write-only, never displayed back).
**5.15 Trust levels** (`/settings/trust`) — per runbook action and domain: Suggest → Run after approval → Run automatically, with evidence ("8 successful approved runs, 0 failures") and a cap set by Domain Root (`GET /v1/action-trust`).

## 6. Real-time contract (SSE)

`GET /v1/stream?access_token=...&teamId=...` — `text/event-stream`. Each message: `event: <type>` and `data: <JSON StreamEvent>`.

| event | data.payload | UI reaction |
| --- | --- | --- |
| `alert.created` | `AlertSummary` | prepend to feed |
| `alert.updated` | `AlertSummary` (counter, lastSeenAt, status) | update row/detail |
| `alert.acked` | `{alertId, by}` | status + timeline |
| `alert.escalated` | `{alertId, level, to}` | status + timeline + toast |
| `alert.resolved` | `{alertId, by}` | status |
| `delivery.status` | `Delivery` | deliveries table |
| `suggestion.ready` | `{alertId, suggestionId}` | fetch `GET /v1/alerts/{id}/suggestion` |
| `approval.requested` | `Approval` | inbox badge + toast |
| `approval.decided` | `Approval` | update |

Reconnect automatically; on reconnect refetch the current list (events may be missed).

## 7. Demo script (what the 5-minute video shows — design for these moments)

1. Problem in numbers (slide, not UI).
2. **Onboarding wizard** with a schema the judges bring: AI mapping → first alert in < 5 min (stopwatch on screen).
3. **Metrics page** during load test: throughput, p50/p99, then a channel is killed → retries → **lost = 0**; 500 identical errors → one alert "×500".
4. Agent (PR reviewer) raises `security.secret_leaked` → hard to on-call → nobody acks → **escalation countdown** → Domain Root gets it. Claude Code asks for approval → **Approvals inbox** → approve.
5. Poisoned log arrives → **suspicious content banner**, AI actions disabled.
6. Resolve → "How did you fix it?" note → next similar alert shows **"Fixed before"** in the AI panel.

## 8. Fixtures / seed data

Demo users (password `demo` for all): `master@demo` (Master), `root.payments@demo`, `root.settlements@demo`, `root.security@demo` (Domain Roots), `anna@demo` (Member, Payments on-call), `piotr@demo` (Member, Settlements on-call), `manager@demo` (Informed).
Domains: Payments, Settlements, Security, Markets. Teams: Team Settlements, Team Pricing, Team Rates, Team Security, cross-domain team "Checkout Project" (Payments + Security).

Sample events (from the task, Appendix A) — use them in MSW fixtures:

```json
{"type":"WORKFLOW_FAILED","workflowId":"WF-20261023-00042","workflowName":"order-settlement","step":"validate-counterparty","severity":"ERROR","environment":"prod","timestamp":"2026-10-23T09:14:02.331Z","error":{"code":"TIMEOUT","message":"Counterparty service did not respond in 5000 ms"}}
```
```json
{"alertname":"HighErrorRate","labels":{"service":"payments-api","region":"eu-west","severity":"critical"},"annotations":{"summary":"Error rate 12% over the last 5 minutes"},"startsAt":"2026-10-23T09:13:00Z","status":"firing"}
```
```json
{"meta":{"schema":"trade.rejected.v2","producer":"trade-gateway","ts":1792833245000},"payload":{"tradeId":"T-998877","desk":"rates","notional":25000000,"currency":"EUR","reason":"LIMIT_BREACH"}}
```

Example rule titles: "Rejected EUR 25000000 — LIMIT_BREACH", "order-settlement failed at validate-counterparty".

## 9. Definition of done for the UI

- `docker compose up` serves the UI on `http://localhost:3000`, logged-in demo works against the real backend.
- Every wave-1 screen works with real SSE updates.
- No AI output is applied without an explicit user click.
- README section: how to run, mocks on/off, what is mocked.
