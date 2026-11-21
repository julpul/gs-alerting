"""`retry_workflow`: ask the workflow engine (mockgw `/workflows/{id}/retry`) to retry a workflow."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.runbooks.actions._http import call, gateway, segment
from app.runbooks.actions.registry import ActionContext, ActionResult, register


class Params(BaseModel):
    workflowId: str = Field(min_length=1)


@register
class RetryWorkflow:
    name = "retry_workflow"
    params_schema = Params
    default_risk = "medium"
    label = "Retry workflow {workflowId}"
    affects_trust = True

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        return await call("POST", gateway(f"/workflows/{segment(params.workflowId)}/retry"))
