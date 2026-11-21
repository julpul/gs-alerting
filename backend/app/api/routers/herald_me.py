"""Herald UI: who is calling (`/api/me`), pickers (`/api/meta`), teams and people (`/api/directory`), and a
/v1 token for the current demo identity (`/api/session-token`)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.api.deps import Pool
from app.config import get_settings
from app.core.rabbit import CHANNELS
from app.core.scope import Scope
from app.core.security import create_token
from app.escalation.oncall import current_oncall
from app.escalation.policies import effective_policy
from app.herald import views
from app.herald.identity import HERALD_ROLES, Actor, person_id
from app.herald.ids import people_refs
from app.herald.routing import herald_router
from app.repositories import org
from app.runbooks.actions.registry import actions

router = herald_router(prefix="/api", tags=["herald"])
VERSION = "gs-alerting-bff/0.1"


@router.get("/me")
async def me(who: Actor, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT slug FROM teams WHERE id = ANY($1) ORDER BY slug", list(who.scope.team_ids)
        )
    return {
        "mode": get_settings().auth_mode,
        "user": who.ref,
        "name": who.user["display_name"],
        "role": who.herald_role,
        "teams": [r["slug"] for r in rows],
        "provider": who.session["provider"] if who.session else None,
        "auth_time": datetime.fromtimestamp(who.session["authTime"], tz=UTC).isoformat()
        if who.session
        else None,
        "email": who.user["email"],
        "userId": str(who.user_id),
    }


@router.get("/meta")
async def meta() -> dict[str, Any]:
    settings = get_settings()
    return {
        "severities": views.SEVERITY_GUIDE,
        "operators": views.HERALD_OPERATORS,
        "channels": list(CHANNELS),
        "actions": [
            {"name": name, "risk": spec.default_risk, "description": spec.label}
            for name, spec in sorted(actions().items())
        ],
        "roles": ["viewer", *HERALD_ROLES],
        "version": VERSION,
        "auth": {"mode": settings.auth_mode, "sso": settings.auth_mode == "oidc"},
        "links": {
            "mailpit": settings.public_mailpit_url,
            "mockgw": settings.public_mockgw_url,
            "phone": f"{settings.public_mockgw_url.rstrip('/')}/phone",
            "api": settings.public_base_url,
        },
    }


async def _leads(conn: asyncpg.Connection) -> dict[UUID, UUID]:
    """Team -> "lead" = first Domain Root (by e-mail) of the team's domains: the person who approves."""
    rows = await conn.fetch(
        "SELECT DISTINCT ON (td.team_id) td.team_id, ra.user_id FROM team_domains td "
        "JOIN role_assignments ra ON ra.role = 'domain_root' AND ra.scope_type = 'domain' "
        "AND ra.scope_id = td.domain_id JOIN users u ON u.id = ra.user_id ORDER BY td.team_id, u.email"
    )
    return {r["team_id"]: r["user_id"] for r in rows}


async def _team_preferences(
    conn: asyncpg.Connection, team_id: UUID, domain_id: UUID | None
) -> dict[str, Any]:
    """First escalation level's channels per severity (team > domain > org policy)."""
    out: dict[str, list[str]] = {}
    if domain_id is None:
        return out
    for severity in views.SEVERITIES:
        policy = await effective_policy(
            conn, rule=None, team_id=team_id, domain_id=domain_id, severity=severity
        )
        level = policy.level_for(1)
        if level is not None:
            out[severity] = [str(c) for c in level.channels]
    return out


async def _teams(conn: asyncpg.Connection, scope: Scope, refs: dict[UUID, str]) -> list[dict[str, Any]]:
    teams = await org.list_teams(conn, scope)
    ids = [UUID(t["id"]) for t in teams]
    slugs = {
        r["id"]: r["slug"] for r in await conn.fetch("SELECT id, slug FROM teams WHERE id = ANY($1)", ids)
    }
    with_rules = {r["team_id"] for r in await conn.fetch("SELECT DISTINCT team_id FROM rules")}
    rotations = {
        r["team_id"]: r
        for r in await conn.fetch(
            "SELECT DISTINCT ON (team_id) team_id, member_ids, shift_hours FROM rotations "
            "WHERE team_id = ANY($1) ORDER BY team_id, name",
            ids,
        )
    }
    leads = await _leads(conn)
    pid = lambda uid: refs.get(uid, "").removeprefix("person:") or None  # noqa: E731
    out = []
    for team in teams:
        team_id = UUID(team["id"])
        rotation = rotations.get(team_id)
        oncall = await current_oncall(conn, team_id)
        domain = UUID(team["domainIds"][0]) if team["domainIds"] else None
        out.append(
            {
                "id": slugs[team_id],
                "uuid": team["id"],
                "name": team["name"],
                "members": [p for p in (pid(UUID(m)) for m in team["memberIds"]) if p],
                "lead": pid(leads[team_id]) if team_id in leads else None,
                "on_call": pid(oncall[0]) if oncall else None,
                "rota": [p for p in (pid(m) for m in (rotation["member_ids"] if rotation else [])) if p],
                "shift_days": round(rotation["shift_hours"] / 24, 2) if rotation else None,
                "preferences": await _team_preferences(conn, team_id, domain),
                "has_rules": team_id in with_rules,
            }
        )
    return out


@router.get("/directory")
async def directory(who: Actor, pool: Pool) -> dict[str, Any]:
    """Teams in the caller's scope and the people around them. Names only: no phones or addresses."""
    async with pool.acquire() as conn:
        users = await org.list_users(conn, who.scope)
        user_ids = [UUID(u["id"]) for u in users]
        extra = await conn.fetch(
            "SELECT u.id, u.timezone, p.by_severity FROM users u LEFT JOIN preferences p ON p.user_id = u.id "
            "WHERE u.id = ANY($1)",
            user_ids,
        )
        everyone = {r["user_id"] for r in await conn.fetch("SELECT user_id FROM team_members")}
        everyone |= {r["user_id"] for r in await conn.fetch("SELECT user_id FROM role_assignments")}
        refs = await people_refs(conn, everyone | set(user_ids))
        teams = await _teams(conn, who.scope, refs)
    info = {r["id"]: r for r in extra}
    people = [
        {
            "id": person_id(u["email"]),
            "name": u["displayName"],
            "tz": info[UUID(u["id"])]["timezone"] if UUID(u["id"]) in info else None,
            "preferences": (info[UUID(u["id"])]["by_severity"] or {}) if UUID(u["id"]) in info else {},
            "has_rules": False,
            "uuid": u["id"],
        }
        for u in users
    ]
    return {"teams": teams, "people": people}


@router.get("/session-token")
async def session_token(who: Actor) -> dict[str, Any]:
    """Demo only: a normal /v1 access token for the current Herald identity.

    The React screens use it to call `/v1/*` and `/v1/stream` as the person chosen under "Viewing as"."""
    return {
        "accessToken": create_token(who.user_id, "access"),
        "tokenType": "Bearer",
        "expiresIn": get_settings().access_token_ttl_seconds,
    }
