"""`open_ticket`: create a Jira-like ticket (mockgw `/tickets`)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.runbooks.actions._http import call, gateway
from app.runbooks.actions.registry import ActionContext, ActionResult, register


class Params(BaseModel):
    summary: str = Field(min_length=1, max_length=300)
    description: str = ""
    priority: str | None = None


@register
class OpenTicket:
    name = "open_ticket"
    params_schema = Params
    default_risk = "low"
    label = "Open a ticket: {summary}"
    affects_trust = True

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        body = {
            **{k: v for k, v in params.model_dump().items() if v},
            "alertId": str(ctx.alert.get("id")),
        }
        return await call("POST", gateway("/tickets"), json=body)
