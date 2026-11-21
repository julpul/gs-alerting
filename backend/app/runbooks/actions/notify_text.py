"""`notify_text`: no-op that records a human instruction as done (used for text-like action steps)."""

from __future__ import annotations

from pydantic import BaseModel

from app.runbooks.actions.registry import ActionContext, ActionResult, register


class Params(BaseModel):
    text: str = ""


@register
class NotifyText:
    name = "notify_text"
    params_schema = Params
    default_risk = "low"
    label = "{text}"
    affects_trust = False

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        return ActionResult(True, params.text or "noted")
