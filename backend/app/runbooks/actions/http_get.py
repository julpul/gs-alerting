"""`http_get`: a real HTTP GET (health checks).

Demo convention: a host without dots (`http://counterparty-service/health`) is an internal service name
of the fictional org, served by the gateway at `{MOCKGW_URL}/svc/<name><path>`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel

from app.runbooks.actions._http import call, gateway, validate_external_url
from app.runbooks.actions.registry import ActionContext, ActionResult, register


class Params(BaseModel):
    url: str


def resolve_url(url: str) -> str:
    parts = validate_external_url(url)
    if "." not in (parts.hostname or "") and not parts.netloc.startswith("["):
        path = parts.path or "/health"
        query = f"?{parts.query}" if parts.query else ""
        return gateway(f"/svc/{parts.hostname}{path}{query}")
    return urlsplit(url).geturl()


@register
class HttpGet:
    name = "http_get"
    params_schema = Params
    default_risk = "low"
    label = "Check {url}"
    affects_trust = False

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        try:
            target = resolve_url(params.url)
        except ValueError as exc:
            return ActionResult(False, str(exc))
        return await call("GET", target)
