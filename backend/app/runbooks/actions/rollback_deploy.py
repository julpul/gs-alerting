"""`rollback_deploy`: roll the last deployment back (mockgw `/rollback`)."""

from __future__ import annotations

from pydantic import BaseModel

from app.runbooks.actions._http import call, gateway
from app.runbooks.actions.registry import ActionContext, ActionResult, register


class Params(BaseModel):
    service: str | None = None
    version: str | None = None


@register
class RollbackDeploy:
    name = "rollback_deploy"
    params_schema = Params
    default_risk = "high"
    label = "Roll back the last deployment {service}"
    affects_trust = True

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        body = {k: v for k, v in params.model_dump().items() if v}
        return await call("POST", gateway("/rollback"), json=body)
