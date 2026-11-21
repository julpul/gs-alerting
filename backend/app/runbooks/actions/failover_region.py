"""`failover_region`: fail traffic over to another region (mockgw `/failover`)."""

from __future__ import annotations

from pydantic import BaseModel

from app.runbooks.actions._http import call, gateway
from app.runbooks.actions.registry import ActionContext, ActionResult, register


class Params(BaseModel):
    region: str | None = None
    service: str | None = None


@register
class FailoverRegion:
    name = "failover_region"
    params_schema = Params
    default_risk = "high"
    label = "Fail traffic over {region}"
    affects_trust = True

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        body = {k: v for k, v in params.model_dump().items() if v}
        return await call("POST", gateway("/failover"), json=body)
