"""Personal-access-token authentication for `/mcp` (spec 14 / 16).

`Authorization: Bearer pat_...` -> token row (hashed, not revoked) -> user -> scope. The middleware answers
`401` for anything else and stores the `Principal` in the ASGI scope, so every tool call (each one is its own
HTTP request, even inside a long-lived MCP session) is authenticated again: revoking a token takes effect on
the next call.
"""

from __future__ import annotations

import asyncpg
import orjson
from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.scope import load_scope
from app.core.security import PAT_PREFIX, hash_token
from app.mcp.context import Principal, agent_actor
from app.schemas.internal import User

PRINCIPAL_KEY = "gs_principal"
USER_COLUMNS = "id, email, display_name, phone, manager_id, timezone"


async def authenticate_pat(pool: asyncpg.Pool, token: str) -> Principal | None:
    if not token.startswith(PAT_PREFIX):
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE personal_tokens SET last_used_at = now() "
            "WHERE token_hash = $1 AND revoked_at IS NULL RETURNING id, user_id, name",
            hash_token(token),
        )
        if row is None:
            return None
        user_row = await conn.fetchrow(f"SELECT {USER_COLUMNS} FROM users WHERE id = $1", row["user_id"])
        if user_row is None:
            return None
        user = User(**dict(user_row))
        scope = await load_scope(conn, user.id)
    return Principal(user, scope, agent_actor(user, actor_id=row["id"], name=row["name"]))


def bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    for key, value in headers:
        if key.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
    return None


class PatAuthMiddleware:
    """Pure ASGI wrapper; `app.state.pool` is read per request (it exists once the lifespan ran)."""

    def __init__(self, inner: ASGIApp, app: FastAPI) -> None:
        self.inner = inner
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        token = bearer_token(scope["headers"])
        principal = await authenticate_pat(self.app.state.pool, token) if token else None
        if principal is None:
            await _unauthorized(send)
            return
        scope[PRINCIPAL_KEY] = principal
        await self.inner(scope, receive, send)


async def _unauthorized(send: Send) -> None:
    body = orjson.dumps(
        {"code": "unauthorized", "message": "a valid personal access token is required (Bearer pat_...)"}
    )
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="gs-alerting"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
