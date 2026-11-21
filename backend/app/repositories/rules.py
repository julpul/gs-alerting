"""Rules persistence. Reads are scope-filtered by `team_id` (never by domain)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from app.core.audit import audit, user_actor
from app.core.ids import parse_uuid, uuid7
from app.core.scope import Scope
from app.pipeline.rules.model import RuleDef
from app.schemas.internal import User

RULE_COLUMNS = "id, team_id, name, enabled, version, definition, created_by, updated_at"


def actor_wire(user_id: UUID | None, name: str | None) -> dict[str, Any]:
    if user_id is None:
        return {"kind": "system", "id": "system", "displayName": "system"}
    return {"kind": "user", "id": str(user_id), "displayName": name or str(user_id)}


async def resolve_team(conn: asyncpg.Connection, ref: str | None) -> UUID | None:
    """Team uuid from a uuid or a slug; None when no such team."""
    if not ref:
        return None
    team_id = parse_uuid(ref)
    row = await conn.fetchrow(
        "SELECT id FROM teams WHERE id = $1 OR slug = $2", team_id or UUID(int=0), str(ref)
    )
    return row["id"] if row else None


async def team_slug(conn: asyncpg.Connection, team_id: UUID) -> str | None:
    return await conn.fetchval("SELECT slug FROM teams WHERE id = $1", team_id)


async def can_edit_team(conn: asyncpg.Connection, scope: Scope, team_id: UUID) -> bool:
    """Master: all teams. Domain root: teams of their domains. Member: their own team. Informed: read-only."""
    if scope.is_master:
        return True
    if any(r.role == "member" and r.scope_type == "team" and r.scope_id == team_id for r in scope.roles):
        return True
    if await conn.fetchval(
        "SELECT 1 FROM team_members WHERE team_id = $1 AND user_id = $2", team_id, scope.user_id
    ):
        return True
    root_domains = [r.scope_id for r in scope.roles if r.role == "domain_root" and r.scope_type == "domain"]
    if not root_domains:
        return False
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM team_domains WHERE team_id = $1 AND domain_id = ANY($2)", team_id, root_domains
        )
    )


async def list_rules(conn: asyncpg.Connection, scope: Scope, team_id: UUID | None) -> list[dict[str, Any]]:
    team_ids = [team_id] if team_id is not None else list(scope.team_ids)
    team_ids = [t for t in team_ids if scope.can_see_team(t)]
    rows = await conn.fetch(
        "SELECT r.id, r.name, r.team_id, r.enabled, r.created_by, u.display_name AS owner_name, "
        "  a.last_fired, COALESCE(a.fires24h, 0) AS fires24h, a.actionability "
        "FROM rules r LEFT JOIN users u ON u.id = r.created_by "
        "LEFT JOIN LATERAL ("
        "  SELECT max(last_seen_at) AS last_fired, "
        "         count(*) FILTER (WHERE first_seen_at > now() - interval '24 hours') AS fires24h, "
        "         avg((acked_at IS NOT NULL OR resolved_at IS NOT NULL)::int)::float8 AS actionability "
        "  FROM alerts WHERE rule_id = r.id) a ON true "
        "WHERE r.team_id = ANY($1) ORDER BY r.name, r.id",
        team_ids,
    )
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "teamId": str(r["team_id"]),
            "enabled": r["enabled"],
            "owner": actor_wire(r["created_by"], r["owner_name"]) if r["created_by"] else None,
            "lastFiredAt": r["last_fired"],
            "fires24h": r["fires24h"],
            "actionability": r["actionability"],
        }
        for r in rows
    ]


async def get_record(conn: asyncpg.Connection, scope: Scope, rule_id: UUID) -> asyncpg.Record | None:
    """The rule row if it exists and its team is visible to the caller."""
    row = await conn.fetchrow(f"SELECT {RULE_COLUMNS} FROM rules WHERE id = $1", rule_id)
    if row is None or not scope.can_see_team(row["team_id"]):
        return None
    return row


async def insert_rule(conn: asyncpg.Connection, rule: RuleDef, user: User) -> RuleDef:
    rule_id = uuid7()
    definition = rule.definition()
    await conn.execute(
        "INSERT INTO rules (id, team_id, name, enabled, version, definition, created_by) "
        "VALUES ($1, $2, $3, $4, 1, $5, $6)",
        rule_id,
        rule.team_id,
        rule.name,
        rule.enabled,
        definition,
        user.id,
    )
    await _add_version(conn, rule_id, 1, definition, user.id)
    await audit(
        conn,
        user_actor(user.id, user.display_name),
        "rule.create",
        "rule",
        str(rule_id),
        {"name": rule.name, "teamId": str(rule.team_id), "version": 1},
    )
    return rule.model_copy(update={"id": rule_id, "version": 1})


async def update_rule(
    conn: asyncpg.Connection, current: asyncpg.Record, rule: RuleDef, user: User
) -> RuleDef:
    definition = rule.definition()
    version = await conn.fetchval(
        "UPDATE rules SET team_id = $2, name = $3, enabled = $4, definition = $5, version = version + 1, "
        "updated_at = now() WHERE id = $1 RETURNING version",
        current["id"],
        rule.team_id,
        rule.name,
        rule.enabled,
        definition,
    )
    await _add_version(conn, current["id"], version, definition, user.id)
    await audit(
        conn,
        user_actor(user.id, user.display_name),
        "rule.update",
        "rule",
        str(current["id"]),
        {"name": rule.name, "version": version},
    )
    return rule.model_copy(update={"id": current["id"], "version": version})


async def delete_rule(conn: asyncpg.Connection, current: asyncpg.Record, user: User) -> None:
    await conn.execute("DELETE FROM rule_versions WHERE rule_id = $1", current["id"])
    await conn.execute("DELETE FROM rules WHERE id = $1", current["id"])
    await audit(
        conn,
        user_actor(user.id, user.display_name),
        "rule.delete",
        "rule",
        str(current["id"]),
        {"name": current["name"], "version": current["version"], "definition": current["definition"]},
    )


async def _add_version(
    conn: asyncpg.Connection, rule_id: UUID, version: int, definition: dict[str, Any], user_id: UUID
) -> None:
    await conn.execute(
        "INSERT INTO rule_versions (rule_id, version, definition, changed_by) VALUES ($1, $2, $3, $4)",
        rule_id,
        version,
        definition,
        user_id,
    )


async def list_versions(conn: asyncpg.Connection, rule_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT v.version, v.changed_by, v.changed_at, u.display_name FROM rule_versions v "
        "LEFT JOIN users u ON u.id = v.changed_by WHERE v.rule_id = $1 ORDER BY v.version DESC",
        rule_id,
    )
    return [
        {
            "version": r["version"],
            "changedBy": actor_wire(r["changed_by"], r["display_name"]),
            "changedAt": r["changed_at"],
        }
        for r in rows
    ]
