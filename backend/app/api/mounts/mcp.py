"""Mounts the MCP server at `/mcp` (streamable HTTP, personal-access-token auth)."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from app.mcp.auth import PatAuthMiddleware
from app.mcp.lifespan import in_own_task
from app.mcp.server import build_server

MCP_PATH = "/mcp"


def mount(app: FastAPI) -> AbstractAsyncContextManager[None]:
    """Add the `/mcp` route to the FastAPI app and return the session manager's lifespan.

    DNS-rebinding protection is off: the endpoint is reachable by hostname behind proxies and every request
    needs a bearer token anyway.
    """
    server = build_server(app)
    inner = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    (route,) = [r for r in inner.routes if isinstance(r, Route) and r.path == MCP_PATH]
    app.router.routes.append(Route(MCP_PATH, endpoint=PatAuthMiddleware(route.endpoint, app)))
    return in_own_task(server.session_manager.run())
