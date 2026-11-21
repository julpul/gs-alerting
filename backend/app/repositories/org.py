"""Org reads (domains, teams, users) filtered by scope, and the role grant rule (spec 16)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from app.core.audit import audit, user_actor
from app.core.errors import AppError, forbidden
from app.core.ids import ORG_ID
from app.core.scope import Scope
from app.schemas.internal import RoleAssignment, User

ROLE_RANK = {"informed": 1, "member": 2, "domain_root": 3, "master": 4}
ROLE_SCOPE = {"master": "org", "domain_root": "domain", "member": "team", "informed": "team"}


async def list_domains(conn: asyncpg.Connection, scope: Scope) -> list[dict[str, Any]]:
    where = "" if scope.is_master else "WHERE d.id = ANY($1)"
    args = [] if scope.is_master else [list(scope.domain_ids)]
    rows = await conn.fetch(
        "SELECT d.id, d.name, COALESCE((SELECT array_agg(ra.user_id::text ORDER BY ra.user_id) "
        "FROM role_assignments ra WHERE ra.role = 'domain_root' AND ra.scope_type = 'domain' "
        f"AND ra.scope_id = d.id), '{{}}') AS root_user_ids FROM domains d {where} ORDER BY d.name",
        *args,
    )
    return [{"id": str(r["id"]), "name": r["name"], "rootUserIds": r["root_user_ids"]} for r in rows]


async def list_teams(conn: asyncpg.Connection, scope: Scope) -> list[dict[str, Any]]:
    where = "" if scope.is_master else "WHERE t.id = ANY($1)"
    args = [] if scope.is_master else [list(scope.team_ids)]
    rows = await conn.fetch(
        "SELECT t.id, t.name, "
        "COALESCE((SELECT array_agg(td.domain_id::text ORDER BY td.domain_id) FROM team_domains td "
        "          WHERE td.team_id = t.id), '{}') AS domain_ids, "
        "COALESCE((SELECT array_agg(tm.user_id::text ORDER BY tm.user_id) FROM team_members tm "
        f"          WHERE tm.team_id = t.id), '{{}}') AS member_ids FROM teams t {where} ORDER BY t.name",
        *args,
    )
    return [
        {"id": str(r["id"]), "name": r["name"], "domainIds": r["domain_ids"], "memberIds": r["member_ids"]}
        for r in rows
    ]


def _sees_phones(scope: Scope) -> bool:
    return scope.is_master or any(r.role == "domain_root" for r in scope.roles)


async def list_users(conn: asyncpg.Connection, scope: Scope) -> list[dict[str, Any]]:
    """Master: everyone. Others: people of the teams in their scope, domain roots of their domains, self."""
    cols = "u.id, u.email, u.display_name, u.phone, u.manager_id"
    if scope.is_master:
        rows = await conn.fetch(f"SELECT {cols} FROM users u ORDER BY u.display_name")
    else:
        rows = await conn.fetch(
            f"SELECT {cols} FROM users u WHERE u.id = $1 "
            "OR u.id IN (SELECT user_id FROM team_members WHERE team_id = ANY($2)) "
            "OR u.id IN (SELECT user_id FROM role_assignments WHERE scope_type = 'team' "
            "            AND scope_id = ANY($2)) "
            "OR u.id IN (SELECT user_id FROM role_assignments WHERE role = 'domain_root' "
            "            AND scope_type = 'domain' AND scope_id = ANY($3)) "
            "ORDER BY u.display_name",
            scope.user_id,
            list(scope.team_ids),
            list(scope.domain_ids),
        )
    phones = _sees_phones(scope)
    return [
        {
            "id": str(r["id"]),
            "email": r["email"],
            "displayName": r["display_name"],
            "phone": r["phone"] if phones or r["id"] == scope.user_id else None,
            "managerId": str(r["manager_id"]) if r["manager_id"] else None,
        }
        for r in rows
    ]


async def _target_covered(conn: asyncpg.Connection, held: RoleAssignment, target: RoleAssignment) -> bool:
    """Does the grantor's assignment `held` cover the target scope?"""
    if held.role == "master":
        return True
    if held.role == "domain_root":
        if target.scope_type == "domain":
            return target.scope_id == held.scope_id
        if target.scope_type == "team":
            return bool(
                await conn.fetchval(
                    "SELECT 1 FROM team_domains WHERE team_id = $1 AND domain_id = $2",
                    target.scope_id,
                    held.scope_id,
                )
            )
        return False
    return held.scope_type == "team" and target.scope_type == "team" and held.scope_id == target.scope_id


async def grant_role(
    conn: asyncpg.Connection, scope: Scope, grantor: User, target_user_id: UUID, target: RoleAssignment
) -> None:
    """Grantor's role must be >= the granted role and their scope must contain the target scope."""
    if ROLE_SCOPE[target.role] != target.scope_type:
        raise AppError(
            422,
            "validation_error",
            f"scopeType: role {target.role} is granted at scope {ROLE_SCOPE[target.role]}",
        )
    allowed = False
    for held in scope.roles:
        if held.role == "informed" or ROLE_RANK[held.role] < ROLE_RANK[target.role]:
            continue
        if await _target_covered(conn, held, target):
            allowed = True
            break
    if not allowed:
        raise forbidden("you cannot grant a role above your own or outside your scope")

    if await conn.fetchval("SELECT 1 FROM users WHERE id = $1", target_user_id) is None:
        raise AppError(404, "not_found", "user not found")
    await _require_scope_exists(conn, target)

    await conn.execute(
        "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_by) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
        target_user_id,
        target.role,
        target.scope_type,
        target.scope_id,
        grantor.id,
    )
    if target.role == "member":
        await conn.execute(
            "INSERT INTO team_members (team_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            target.scope_id,
            target_user_id,
        )
    await audit(
        conn,
        user_actor(grantor.id, grantor.display_name),
        "role.grant",
        "user",
        str(target_user_id),
        {"role": target.role, "scopeType": target.scope_type, "scopeId": str(target.scope_id)},
    )


async def _require_scope_exists(conn: asyncpg.Connection, target: RoleAssignment) -> None:
    if target.scope_type == "org":
        ok = target.scope_id == ORG_ID
    else:
        table = "domains" if target.scope_type == "domain" else "teams"
        ok = await conn.fetchval(f"SELECT 1 FROM {table} WHERE id = $1", target.scope_id) is not None
    if not ok:
        raise AppError(422, "validation_error", "scopeId: scope does not exist")
