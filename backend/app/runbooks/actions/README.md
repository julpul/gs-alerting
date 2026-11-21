# Runbook actions

An action is one module in this package with one class decorated by `@register`. Nothing else to wire:
`registry.load_actions()` imports every module here, the step runner, the parsers and the trust ladder pick
it up by name.

```python
from pydantic import BaseModel
from app.runbooks.actions.registry import ActionContext, ActionResult, register


class Params(BaseModel):  # validated after {{templates}} are rendered from the alert
    host: str


@register
class DrainNode:
    name = "drain_node"  # what runbooks write in `action:`
    params_schema = Params
    default_risk = "high"  # low | medium | high; high always needs approval
    label = "Drain node {host}"  # default step text; {param} placeholders from the step's parameters
    affects_trust = True  # False for read-only checks: failures do not demote trust

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        return ActionResult(ok=True, output="drained")
```

- `ctx.alert` is the alert row (dict), `ctx.ev` the event (`ctx.ev.get("labels.service")`), `ctx.actor` who runs
  it. `ctx.conn` is set so internal actions (like `escalate`) can write to the database; add outbox ids to
  `ctx.outbox_ids` and they are published after the run.
- Return `ok=False` for "the action ran and failed" (recorded as a failed step run, demotes trust). Raised
  exceptions are treated the same way.
- Network calls go through `_http.call` (10 s timeout). Event content is untrusted: build URLs with
  `_http.segment(...)`.

## Built in

| Action | Does | Risk |
| --- | --- | --- |
| `http_get` | real GET. A host without dots (`http://counterparty-service/health`) is served by the gateway at `{MOCKGW_URL}/svc/<host><path>` | low |
| `retry_workflow` | `POST {gw}/workflows/{workflowId}/retry` | medium |
| `restart_service` | `POST {gw}/services/{service}/restart` | high |
| `failover_region` | `POST {gw}/failover` | high |
| `rollback_deploy` | `POST {gw}/rollback` | high |
| `open_ticket` | `POST {gw}/tickets` | low |
| `webhook` | HMAC-signed JSON POST (`X-Signature: sha256=...`) | medium |
| `escalate` | hard SMS/email to another team's on-call (or the domain roots if the team is unknown) | low |
| `notify_text` | no-op, records the instruction | low |

Whether a run needs approval: step `approval: required`, or risk `high`, or a trust level below `auto`
(`app/runbooks/trust.py`). High-risk actions never reach `auto`.
