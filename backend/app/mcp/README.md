# MCP server and in-app copilot

The `api` process serves an MCP server (official `mcp` SDK, streamable HTTP) at `/mcp`, so coding agents can
read alerts, raise typed alerts and ask a human for approval.

## Connect an agent (snippet for the root README)

1. In the UI (or `POST /v1/tokens {"name": "claude-code"}`) create a personal access token. The plain
   `pat_...` value is shown once; only its SHA-256 is stored. Revoke with `DELETE /v1/tokens/{id}`.
2. Add the server:

```bash
claude mcp add --transport http alerting http://localhost:8080/mcp --header "Authorization: Bearer <PAT>"
```

Any MCP client with streamable HTTP works the same way (Cursor, OpenAI Agents SDK, ...).

Every call runs with the permissions of the token's owner. Writes are audited and shown in the alert
timeline as "agent <token name> on behalf of <user>". Target design for production: OAuth 2.1 per the MCP
spec; the personal access token is the hackathon shortcut.

## Tools

| Tool | What it does |
| --- | --- |
| `list_alerts(status?, team?, limit?)`, `get_alert(id)` | Scoped reads; `get_alert` includes runbook state, latest AI suggestion, deliveries, timeline |
| `ack_alert(id)`, `comment_alert(id, text)` | As the user (ack needs member or higher on the team) |
| `raise_alert(type, severity, title, description?, attributes?, dedup_key?, source?)` | Same pipeline as `POST /v1/alerts/raise` |
| `request_approval(action, parameters, reason, risk, alert_id?, timeout_seconds=120)` | Shows an "agent asked" request with Confirm / Cancel on the alert and waits (max 300 s) for `approved` / `rejected` / `expired`, else `{status: pending, approvalId}` |
| `get_approval_status(approval_id)` | Poll |
| `run_runbook_step(alert_id, step_id)` | Runs if low risk and trusted, otherwise creates a request for a human to confirm |
| `propose_rule(text, team)`, `test_rule(rule)` | Rule copilot draft + dry-run (never saved) |
| `propose_schema_mapping(source_id, sample)`, `propose_runbook(text)` | Drafts (never saved) |
| `get_integration_guide()` + resource `docs://api` | Markdown guide with curl/python examples and the OpenAPI contract |

`raise_alert` source: without `source` the alert comes from a service source `agent:<token name>` that is
created on first use in the caller's first team (alphabetical by slug, where they may edit). A rule has to
match that source name (or the alert type) for anyone to be notified; otherwise the event is counted
`unrouted`. Pass `source` (name or id of an existing source of the caller's team, for example
`pr-review-agent`) to reuse a source that already has rules.

## In-app copilot

`POST /v1/agent/chat` streams SSE (`token`, `tool_call`, `tool_result`, `draft`, `done`, `error`). It runs a loop
of at most 6 steps with `LLM_MODEL_SMART` through the shared LLM client (`app.ai.llm`), over the same tool
functions, with the same scope checks. The model can only pick tools from an allow-list (no `raise_alert`),
arguments are validated against the tool schema, and tool results reach the model only inside
`<untrusted_event>` blocks. Acting tools run as actor "Copilot on behalf of <user>".

## Files

- `server.py` builds the MCP server from `registry.py` (tool specs), `auth.py` is the PAT middleware.
- `tools/` implementations (no HTTP knowledge): alerts, approvals, drafts, guide.
- `copilot.py` the chat loop, mounted by `app/api/routers/agent.py`.
- `static/openapi.yaml` is a copy of `docs/openapi.yaml` bundled into the image (a test fails when stale:
  `cp docs/openapi.yaml backend/app/mcp/static/openapi.yaml`).
- The mount itself is `app/api/mounts/mcp.py` (see `discover_mounts` in `app/api/app.py`).
