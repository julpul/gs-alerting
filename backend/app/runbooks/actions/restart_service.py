"""`restart_service`: restart a service through the gateway (mockgw `/services/{name}/restart`)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.runbooks.actions._http import call, gateway, segment
from app.runbooks.actions.registry import ActionContext, ActionResult, register


class Params(BaseModel):
    service: str = Field(min_length=1)


@register
class RestartService:
    name = "restart_service"
    params_schema = Params
    default_risk = "high"
    label = "Restart service {service}"
    affects_trust = True

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        return await call("POST", gateway(f"/services/{segment(params.service)}/restart"))
