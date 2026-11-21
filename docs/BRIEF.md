# Hackathon 2026 – Software Engineering: Flexible Alerting & Notification Service (original brief)

Kept verbatim-in-substance for implementers. `TECH_SPEC.md` maps every point below to the design (§1).

## 1. The Problem
Every system produces errors, warnings and business events (failed workflow, breached threshold, rejected message). Signals end up spread across logs, dashboards and mailboxes; the right people find out late or get so many alerts they stop reading them. Build a service that gets the right alert to the right people, on the channel they prefer, and helps them resolve it by following the team's runbooks.

Keep in mind: **Flexibility** (easily adoptable by any team/system), **Scalability** (high volume must not impact performance), **UX** (engineers, on-call support, potentially non-engineers).

## 2. Functionalities
### 2.1 Ingest from many sources
- At least two source types (message broker — Kafka/RabbitMQ, HTTP/webhook, tailed log file, polled API).
- Messages arrive in different schemas; no fixed format (Appendix A).
- Adding a new source or schema needs as little code as possible.

### 2.2 Let teams define what they care about
- Filters on any field: source, workflow, schema, severity, environment, or message body values (`amount > 1,000,000`, `error.code in [TIMEOUT, REJECTED]`).
- AND/OR combinations.
- Per-rule routing (who, which channels) and per-rule presentation (title/body templates, fields, severity/colour in UI).
- Rules created/changed at runtime (API, UI or live-reloaded config files) without restart.

### 2.3 Deliver alerts on the preferred channel
- Sample channels: Email, SMS, Phone call, UI.
- Per team/person channel preferences (critical → phone + SMS; warning → email; info → UI only). Optional working hours / on-call schedules.
- External channels may be simulated (mock SMTP, fake SMS/phone gateway, sandbox). Judged: adding a real provider is a small isolated change.
- Delivery must survive failure: retries, no lost alerts when a channel is down.

### 2.4 Service UI
Live feed per team (push, not refresh); acknowledge/resolve; manage rules and channel preferences; propose fixes/enhancements to alerts; each rule controls how its alerts look.

### 2.5 Don't flood people
Deduplication (same error 500×/min = one alert with counter); throttling or grouping per rule or recipient.

### 2.6 Help resolve alerts with runbooks
- Runbooks attached to rules, schemas or alert types in many formats (Markdown, wiki/Confluence link, checklist, free text) — Appendix B.
- When an alert fires, show the matching runbook and next steps filled with message values, on every channel where they fit (full steps in UI/email, short link in SMS).
- Steps can be actions: retry workflow, restart service, call webhook, open ticket, escalate. Risky actions need human approval first (UI or reply to the alert).
- Record what was done and by whom; close the alert when the problem goes away.
- Adding a runbook or a new kind of action should be as easy as adding a rule.
- Optional: automatically record resolution steps so the knowledge base updates itself.

## 3. Judging Criteria
Flexibility (new source/schema/channel/rule type: config or code? rule expressiveness) · Scalability (horizontal scale, state handling for rules/dedup windows/delivery status, bottlenecks/SPOF) · UX (usable by a newcomer, understandable rule editor, actionable alerts, fresh clone → demo: how many commands/how long, clear README, time for a new team to get alerts) · **Throughput/Latency — msg/s and end-to-end p50/p99 ingest→delivery, measured not estimated; include load-test script and results** · Level of implementation (repo link, README, what is mocked) · Innovation · Presentation.

## 4. Submission
≤ 5-minute video (primary basis for judging), AI-tools usage + security/privacy disclosure, upload to Lockbox in a folder named after the team, plus a text file with the repo link / demo app. Top 5 present live + Q&A.

## Appendix A — Sample events (judges bring a new schema to the demo)

Workflow failure (message broker):
```json
{"type":"WORKFLOW_FAILED","workflowId":"WF-20261023-00042","workflowName":"order-settlement","step":"validate-counterparty","severity":"ERROR","environment":"prod","timestamp":"2026-10-23T09:14:02.331Z","error":{"code":"TIMEOUT","message":"Counterparty service did not respond in 5000 ms"}}
```
Metric threshold breach (HTTP webhook):
```json
{"alertname":"HighErrorRate","labels":{"service":"payments-api","region":"eu-west","severity":"critical"},"annotations":{"summary":"Error rate 12% over the last 5 minutes"},"startsAt":"2026-10-23T09:13:00Z","status":"firing"}
```
Business event (message broker, different envelope):
```json
{"meta":{"schema":"trade.rejected.v2","producer":"trade-gateway","ts":1792833245000},"payload":{"tradeId":"T-998877","desk":"rates","notional":25000000,"currency":"EUR","reason":"LIMIT_BREACH"}}
```

Example rules in plain words:
- **Team Settlements:** any WORKFLOW_FAILED where workflowName = order-settlement and environment = prod → phone the on-call person + SMS; show the step and the error message in red in the UI.
- **Team Pricing:** log lines from pricing-engine-* at WARN or above → UI only; group into one alert per instrument every 5 minutes.
- **Team Rates:** trade.rejected.* where desk = rates and notional > 10,000,000 → email + UI; title "Rejected {{payload.currency}} {{payload.notional}} — {{payload.reason}}".
- **Anna (personal):** anything critical from payments-api → SMS, but only between 08:00 and 20:00; otherwise email.

## Appendix B — Sample runbooks (judges bring an unseen format)

Structured steps (YAML, Team Settlements):
```yaml
runbook: settlement-timeout
matches:
  type: WORKFLOW_FAILED
  "error.code": TIMEOUT
steps:
  - check: "Is counterparty-service healthy?"
    action: http_get
    url: "http://counterparty-service/health"
  - action: retry_workflow
    workflowId: "{{workflowId}}"
    approval: none
  - if: "retry fails twice"
    action: escalate
    team: counterparty-support
    approval: required
```

Markdown page (payments rule):
```markdown
# HighErrorRate on payments-api
1. Open the payments dashboard and check if errors are in one region only.
2. If one region: fail traffic over to the other region (needs approval from the team lead).
3. If all regions: check the last deployment and roll it back.
4. Post an update in the #payments-incidents channel every 15 minutes.
```

Free text (copied from a wiki or chat):
> When pricing latency goes over a second, it's usually the cache. Restart the pricing-engine instance named in the alert, wait 2 minutes, and if latency is still high, call the pricing on-call person.
