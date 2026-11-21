"""Authorization scope: computed from role assignments, loaded per request.

Scope rules (spec 16):
  master       -> all domains and teams
  domain_root  -> their domains and every team linked to those domains
  member / informed (role assignment on a team, or a `team_members` row) -> those teams, plus the
                  domains those teams belong to (a cross-domain team therefore exposes both domains'
                  ids, but repositories filter alerts/sources/rules by `team_id`).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import Depends, Request
from pydantic import BaseModel, Field

from app.core.errors import AppError, unauthorized
from app.core.security import PAT_PREFIX, AuthError, decode_token, hash_token
from app.schemas.internal import RoleAssignment, User


class Scope(BaseModel):
    user_id: UUID
    is_master: bool = False
    domain_ids: set[UUID] = Field(default_factory=set)
    team_ids: set[UUID] = Field(default_factory=set)
    roles: list[RoleAssignment] = Field(default_factory=list)

    def can_see_team(self, team_id: UUID) -> bool:
        return self.is_master or team_id in self.team_ids

    def can_see_domain(self, domain_id: UUID) -> bool:
        return self.is_master or domain_id in self.domain_ids


async def load_scope(conn: asyncpg.Connection, user_id: UUID) -> Scope:
    rows = await conn.fetch(
        "SELECT role, scope_type, scope_id FROM role_assignments WHERE user_id = $1 ORDER BY role, scope_id",
        user_id,
    )
    roles = [RoleAssignment(role=r["role"], scope_type=r["scope_type"], scope_id=r["scope_id"]) for r in rows]
    if any(r.role == "master" for r in roles):
        domains = {r["id"] for r in await conn.fetch("SELECT id FROM domains")}
        teams = {r["id"] for r in await conn.fetch("SELECT id FROM teams")}
        return Scope(user_id=user_id, is_master=True, domain_ids=domains, team_ids=teams, roles=roles)

    domain_ids = {r.scope_id for r in roles if r.role == "domain_root" and r.scope_type == "domain"}
    team_ids = {r.scope_id for r in roles if r.scope_type == "team"}
    if domain_ids:
        rows = await conn.fetch(
            "SELECT team_id FROM team_domains WHERE domain_id = ANY($1)", list(domain_ids)
        )
        team_ids |= {r["team_id"] for r in rows}
    rows = await conn.fetch("SELECT team_id FROM team_members WHERE user_id = $1", user_id)
    team_ids |= {r["team_id"] for r in rows}
    if team_ids:
        rows = await conn.fetch("SELECT domain_id FROM team_domains WHERE team_id = ANY($1)", list(team_ids))
        domain_ids |= {r["domain_id"] for r in rows}
    return Scope(user_id=user_id, domain_ids=domain_ids, team_ids=team_ids, roles=roles)


_USER_COLUMNS = "id, email, display_name, phone, manager_id, timezone"


async def _user_by_id(conn: asyncpg.Connection, user_id: UUID) -> User | None:
    row = await conn.fetchrow(f"SELECT {_USER_COLUMNS} FROM users WHERE id = $1", user_id)
    return User(**dict(row)) if row else None


async def _user_by_pat(conn: asyncpg.Connection, token: str) -> User | None:
    row = await conn.fetchrow(
        "UPDATE personal_tokens SET last_used_at = now() "
        "WHERE token_hash = $1 AND revoked_at IS NULL RETURNING user_id",
        hash_token(token),
    )
    return await _user_by_id(conn, row["user_id"]) if row else None


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.query_params.get("access_token")


async def current_user(request: Request) -> User:
    """Bearer JWT, Bearer personal access token (`pat_...`) or `?access_token=` (SSE)."""
    token = _extract_token(request)
    if not token:
        raise unauthorized()
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        if token.startswith(PAT_PREFIX):
            user = await _user_by_pat(conn, token)
        else:
            try:
                claims = decode_token(token, "access")
                user = await _user_by_id(conn, UUID(claims["sub"]))
            except (AuthError, ValueError) as exc:
                raise AppError(401, "invalid_token", "invalid or expired token") from exc
    if user is None:
        raise AppError(401, "invalid_token", "invalid or expired token")
    return user


async def current_scope(request: Request, user: Annotated[User, Depends(current_user)]) -> Scope:
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        return await load_scope(conn, user.id)


CurrentUser = Annotated[User, Depends(current_user)]
CurrentScope = Annotated[Scope, Depends(current_scope)]
