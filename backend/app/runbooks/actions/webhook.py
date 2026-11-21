"""`webhook`: POST JSON to any URL, signed with HMAC-SHA256 (`X-Signature: sha256=<hex>`)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import orjson
from pydantic import BaseModel

from app.config import get_settings
from app.runbooks.actions._http import call, validate_external_url
from app.runbooks.actions.registry import ActionContext, ActionResult, register


class Params(BaseModel):
    url: str
    body: dict[str, Any] = {}
    secret: str | None = None


@register
class Webhook:
    name = "webhook"
    params_schema = Params
    default_risk = "medium"
    label = "Call webhook {url}"
    affects_trust = True

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        try:
            validate_external_url(params.url)
        except ValueError as exc:
            return ActionResult(False, str(exc))
        payload = orjson.dumps(
            {"alertId": str(ctx.alert.get("id")), "stepId": ctx.step_id, "body": params.body}
        )
        key = (params.secret or get_settings().app_secret).encode()
        signature = "sha256=" + hmac.new(key, payload, hashlib.sha256).hexdigest()
        return await call(
            "POST",
            params.url,
            content=payload,
            headers={"Content-Type": "application/json", "X-Signature": signature},
        )
