"""Personal access tokens for MCP clients and agents (spec 14/16): hashed at rest, shown once, revocable."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, Pool
from app.core.audit import audit, user_actor
from app.core.errors import AppError, ORJSONResponse, not_found
from app.core.ids import parse_uuid, uuid7
from app.core.security import generate_pat
from app.schemas import api as wire

router = APIRouter(prefix="/v1/tokens", tags=["tokens"])

COLUMNS = "id, name, prefix, created_at, last_used_at"


class CreateToken(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def token_wire(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "prefix": row["prefix"],
        "createdAt": row["created_at"],
        "lastUsedAt": row["last_used_at"],
    }


@router.get("", response_model=list[wire.PersonalToken])
async def list_tokens(user: CurrentUser, pool: Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"SELECT {COLUMNS} FROM personal_tokens WHERE user_id = $1 AND revoked_at IS NULL "
        "ORDER BY created_at DESC, id DESC",
        user.id,
    )
    return [token_wire(r) for r in rows]


@router.post("", status_code=201, response_model=wire.PersonalTokenCreated)
async def create_token(body: CreateToken, user: CurrentUser, pool: Pool) -> Response:
    name = body.name.strip()
    if not name:
        raise AppError(422, "validation_error", "name: must not be blank")
    token, prefix, token_hash = generate_pat()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO personal_tokens (id, user_id, token_hash, name, prefix) "
            f"VALUES ($1, $2, $3, $4, $5) RETURNING {COLUMNS}",
            uuid7(),
            user.id,
            token_hash,
            name,
            prefix,
        )
        await audit(
            conn,
            user_actor(user.id, user.display_name),
            "token.create",
            "token",
            str(row["id"]),
            {"name": name},
        )
    return ORJSONResponse({**token_wire(row), "token": token}, status_code=201)


@router.delete("/{token_id}", status_code=204)
async def revoke_token(token_id: str, user: CurrentUser, pool: Pool) -> Response:
    ident = parse_uuid(token_id)
    if ident is None:
        raise not_found("token")
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "UPDATE personal_tokens SET revoked_at = now() "
            "WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL RETURNING id, name",
            ident,
            user.id,
        )
        if row is None:
            raise not_found("token")
        await audit(
            conn,
            user_actor(user.id, user.display_name),
            "token.revoke",
            "token",
            str(row["id"]),
            {"name": row["name"]},
        )
    return Response(status_code=204)
