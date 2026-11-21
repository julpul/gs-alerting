# Goldman Sachs "Must" checklist — status and evidence

Source: `Alerting Service Team Checklist - Must.csv` (M = required functionality, J = judging evidence, S = submission).
Status legend: ✅ done + verified · 🟡 done, verification pending · ⏳ in progress · 👤 needs the team (not automatable).
UI = Herald web UI served by our api (`/` classic, `/ads/` React); API = `/v1` (openapi) unless noted.

| ID | Requirement | Status | Evidence (code · tests · how to show) |
| --- | --- | --- | --- |
| M01 | ≥ 2 source types | ✅ | Webhook `POST /v1/ingest/{key}` (+ batch, typed raise), RabbitMQ connector role (`app/connectors/rabbitmq.py`, simulator publishes `vistula.trade-gateway`), Alertmanager/Sentry/Datadog/CloudEvents presets · `tests/connectors/*`, `tests/pipeline/test_ingest.py` |
| M02 | Different shapes + unseen schema | ✅ | Schema detection + per-source mapping (`app/pipeline/{schema_detect,mapping,normalize}.py`), rules can match raw paths without mapping, AI mapping suggest + heuristic fallback · `tests/pipeline/test_schema_detect.py`, `tests/ai/test_mapping_rules_runbooks.py`; demo: `demo-key-custody` source / simulator `new_schema` |
| M03 | New source/schema ≈ no code | ✅ | Source = config (`POST /v1/sources`, UI Platforms → Connect), mapping = data (`PUT /v1/sources/{id}/mapping`), new protocol = one connector class (`app/connectors/README.md`) |
| M04 | Filter on any field incl. metadata + nested payload | ✅ | Operators eq/neq/gt/gte/lt/lte/in/not_in/contains/regex/glob/exists on raw dotted paths and `_meta` fields · `tests/rules/test_compiler.py` (65 cases) |
| M05 | AND / OR | ✅ | Nested condition groups · `tests/rules/test_compiler.py::test_nested_and_or_groups` |
| M06 | Recipients + channels per rule | ✅ | `routing.hard/soft`, `channelsBySeverity`, `timeWindow` · `tests/delivery/test_channel_select.py`, `test_routing.py` |
| M07 | Title/body templates, fields, severity/colour per rule, shown in UI | ✅ | `presentation` (`app/pipeline/rules/templates.py`, `{{path}}` only) · `tests/rules/test_templates.py`; UI alert list/detail colour + fields |
| M08 | Rules at runtime, no restart | ✅ | CRUD + versions + YAML, Redis pub/sub hot reload ≤ 2 s · `tests/api/test_rules_api.py::test_update_is_visible_to_a_rule_index_within_two_seconds`; UI rule editor |
| M09 | Team/person channel preferences used | ✅ | `preferences` table (bySeverity + working hours), effective escalation policy per team · `tests/delivery/test_channel_select.py`, `tests/escalation/test_policies_api.py`; UI Teams / preferences |
| M10 | Replaceable adapters; UI + simulated email/SMS/phone | ✅ | `app/delivery/channels/*` + `@register` registry, Mailpit, mockgw · `tests/delivery/*`, `app/delivery/channels/README.md` |
| M11 | Retries + no loss during channel downtime | ✅ | Retry queues 5 s/30 s → DLQ → fallback channel · T4 chaos **LOST = 0** (`loadtest/results/`), `tests/delivery/test_worker.py::test_failing_sms_retries_then_dead_letters_and_falls_back_to_email` |
| M12 | Live feed via push | ✅ | SSE `/v1/stream` + UI `/api/stream` · `tests/api/test_stream.py`, `tests/herald/test_core_*` |
| M13 | Ack / resolve in UI | ✅ | `/v1/alerts/{id}/ack|resolve`, SMS `ACK n`, MCP `ack_alert` · `tests/api/test_alerts.py`, `tests/escalation/test_inbound_sms.py` |
| M14 | Manage rules + channel preferences in UI | ✅ | Herald UI rule editor + preferences via BFF (`app/api/routers/herald_*`) |
| M15 | Propose fixes / enhancements to alerts | 🟡 | AI suggestion panel (Accept/Reject), comments/notes, Copilot `propose_rule` / runbook drafts · `tests/ai/test_suggest.py` |
| M16 | Dedup with counter | ✅ | Fingerprint + partial unique index + UPDATE fast path · T6: 500 → **1 alert ×500**, 1 phone + 1 SMS · `tests/pipeline/test_worker.py::test_500_identical_events_*` |
| M17 | Throttling / grouping per rule or recipient | ✅ | `grouping.byField + windowSeconds` (Pricing per instrument / 5 min), per-recipient throttle → digest email · `tests/delivery/test_routing.py::test_throttle_*`, `tests/escalation/test_digest.py` |
| M18 | Runbooks attached to rules/schemas/types | ✅ | `rule.runbookId`, `attached.matches`, schema/type lookup · `tests/runbooks/test_lookup.py` |
| M19 | Many formats + unseen runbook | ✅ | YAML, Markdown, free text (AI + heuristic), link, checklist, JSON, auto-detect · `tests/runbooks/test_parsers.py` |
| M20 | Matching runbook + steps with event values on fitting channels | ✅ | Templated steps in UI/email, short link in SMS · `tests/runbooks/test_lookup.py::test_lookup_by_attached_matches_renders_event_values`, `app/delivery/render.py` |
| M21 | Executable steps (retry workflow, escalate) | ✅ | Action registry (`http_get`, `retry_workflow`, `restart_service`, `escalate`, `open_ticket`, `failover_region`, `rollback_deploy`, `webhook`) · `tests/runbooks/test_flow.py` |
| M22 | Human approval before risky action, from alert detail | ✅ | Approvals (UI, SMS `APPROVE n`, MCP) · `tests/runbooks/test_flow.py::test_settlement_runbook_check_retry_then_escalate_with_approval`; UI alert detail + Approvals inbox |
| M23 | Record what/who; close when problem goes away | ✅ | timeline + `step_runs` + `audit_log`; auto-resolve on source `resolved` (Alertmanager) · `tests/connectors/test_presets*.py`, `tests/runbooks/*` |
| M24 | New runbook / action ≈ adding a rule | ✅ | Runbook = data (`POST /v1/runbooks`, parse any format); action = one file + `@register` (`app/runbooks/actions/README.md`) |
| J01 | How to add source/schema/channel/rule type; config vs code; expressive rules | ✅ | README "Flexibility: how to add" table |
| J02 | Horizontal scaling, state, bottlenecks/SPOF | ✅ | README "Scalability", `docs/ARCHITECTURE.md` (failure modes) |
| J03 | Fresh clone → demo: commands, time, time-to-first-alert, first-time-user check | 👤 | README quick start (3 commands); stopwatch + first-time-user test by the team |
| J04 | msg/s + p50/p99 measured, script + results in presentation | ✅ | `loadtest/` (k6 + chaos + report.py), results in `loadtest/results/`, README table (T1 p50 5.6 ms / p99 52 ms, T2 1,500–2,500 ev/s, T4 LOST 0) |
| J05 | Repo link + README with what is mocked | 🟡 | README "What is mocked"; repo visibility for judges 👤 |
| S01 | ≤ 5 min video | 👤 | Script: TECH_SPEC §21 |
| S02 | Upload to Lockbox / team folder | 👤 | — |
| S03 | AI tools usage + security/privacy | ✅ | README "AI usage & security disclosure", `docs/AGENTS.md` |

## QA gate (2026-11-21 ~05:00, local stack + Railway, Playwright through both UIs)
- Verified end to end through the UI: live feed in two tabs, ack/resolve without refresh, classic rule editor (nested AND/OR + numeric) live without restart, SMS outage → retry → DLQ → email fallback (nothing lost), dedup 500 → one alert ×500, Pricing grouping per instrument, Appendix B runbooks + 8 unseen runbooks parsed, executable steps (http_get, retry_workflow), risky step approved by a *different* person (four-eyes) from alert detail and Approvals screen, Alertmanager `resolved` auto-close, MCP: PAT from UI → initialize/tools/list/list_alerts → revoke → 401, Railway UI identical.
- Unseen schemas S1–S9 accepted (S3 text and S9 top-level array via `/v1/ingest` being fixed in FIX-BE).
- After FIX-UI/FIX-BE (05:30): M14 ✅ (`/ads/#/preferences`, team channel policy editor, classic Teams table); S3 text logs + S9 arrays accepted on `/v1/ingest`; wildcard paths; four-eyes approvers. Open: M15 AI Accept/Reject needs an LLM key (manual AI test by the team).
