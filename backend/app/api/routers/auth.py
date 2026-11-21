"""POST /v1/auth/login, GET /v1/auth/me."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentScope, CurrentUser, Pool
from app.config import get_settings
from app.core.errors import AppError
from app.core.security import create_token, hash_password, verify_password
from app.schemas import api as wire

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Used to equalise timing when the e-mail is unknown.
_DUMMY_HASH: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    accessToken: str
    refreshToken: str
    expiresIn: int


def _invalid() -> AppError:
    return AppError(401, "invalid_credentials", "invalid email or password")


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, pool: Pool) -> LoginResponse:
    global _DUMMY_HASH
    row = await pool.fetchrow(
        "SELECT id, password_hash FROM users WHERE lower(email) = lower($1)", body.email.strip()
    )
    if row is None:
        _DUMMY_HASH = _DUMMY_HASH or hash_password("dummy")
        verify_password(body.password, _DUMMY_HASH)
        raise _invalid()
    if not verify_password(body.password, row["password_hash"]):
        raise _invalid()
    return LoginResponse(
        accessToken=create_token(row["id"], "access"),
        refreshToken=create_token(row["id"], "refresh"),
        expiresIn=get_settings().access_token_ttl_seconds,
    )


@router.get("/me", response_model=wire.Me)
async def me(user: CurrentUser, scope: CurrentScope, pool: Pool) -> wire.Me:
    team_ids = list(scope.team_ids)
    teams = await pool.fetch(
        "SELECT t.id, t.name, "
        "  COALESCE((SELECT array_agg(td.domain_id::text ORDER BY td.domain_id) FROM team_domains td "
        "            WHERE td.team_id = t.id), '{}') AS domain_ids, "
        "  COALESCE((SELECT array_agg(tm.user_id::text ORDER BY tm.user_id) FROM team_members tm "
        "            WHERE tm.team_id = t.id), '{}') AS member_ids "
        "FROM teams t WHERE t.id = ANY($1) ORDER BY t.name",
        team_ids,
    )
    return wire.Me(
        user=wire.User(
            id=str(user.id),
            email=user.email,
            displayName=user.display_name,
            phone=user.phone,
            managerId=str(user.manager_id) if user.manager_id else None,
        ),
        roles=[
            wire.RoleAssignment(role=r.role, scopeType=r.scope_type, scopeId=str(r.scope_id))
            for r in scope.roles
        ],
        teams=[
            wire.Team(id=str(t["id"]), name=t["name"], domainIds=t["domain_ids"], memberIds=t["member_ids"])
            for t in teams
        ],
    )
