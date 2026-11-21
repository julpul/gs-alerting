"""Org API: domains, teams, users, role grants (spec 15/16)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentScope, CurrentUser, Pool
from app.core.errors import AppError
from app.core.ids import parse_uuid
from app.repositories import org as repo
from app.schemas import api as wire
from app.schemas.internal import RoleAssignment

router = APIRouter(prefix="/v1", tags=["org"])


@router.get("/domains", response_model=list[wire.Domain])
async def list_domains(scope: CurrentScope, pool: Pool) -> list[dict]:
    async with pool.acquire() as conn:
        return await repo.list_domains(conn, scope)


@router.get("/teams", response_model=list[wire.Team])
async def list_teams(scope: CurrentScope, pool: Pool) -> list[dict]:
    async with pool.acquire() as conn:
        return await repo.list_teams(conn, scope)


@router.get("/users", response_model=list[wire.User])
async def list_users(scope: CurrentScope, pool: Pool) -> list[dict]:
    async with pool.acquire() as conn:
        return await repo.list_users(conn, scope)


@router.post("/users/{user_id}/roles", response_model=wire.RoleAssignment)
async def grant_role(
    user_id: str, body: wire.RoleAssignment, user: CurrentUser, scope: CurrentScope, pool: Pool
) -> wire.RoleAssignment:
    target_user = parse_uuid(user_id)
    scope_id = parse_uuid(body.scopeId)
    if target_user is None:
        raise AppError(404, "not_found", "user not found")
    if scope_id is None:
        raise AppError(422, "validation_error", "scopeId: must be a uuid")
    target = RoleAssignment(role=body.role.value, scope_type=body.scopeType.value, scope_id=scope_id)
    async with pool.acquire() as conn, conn.transaction():
        await repo.grant_role(conn, scope, user, target_user, target)
    return body
