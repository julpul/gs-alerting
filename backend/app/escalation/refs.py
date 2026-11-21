"""Lookup of teams, domains and users by wire reference (uuid or slug / email)."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from app.core.ids import parse_uuid


async def team_id(conn: asyncpg.Connection, ref: str) -> UUID | None:
    uid = parse_uuid(ref)
    if uid is not None:
        return await conn.fetchval("SELECT id FROM teams WHERE id = $1", uid)
    return await conn.fetchval("SELECT id FROM teams WHERE slug = $1", ref)


async def domain_id(conn: asyncpg.Connection, ref: str) -> UUID | None:
    uid = parse_uuid(ref)
    if uid is not None:
        return await conn.fetchval("SELECT id FROM domains WHERE id = $1", uid)
    return await conn.fetchval("SELECT id FROM domains WHERE slug = $1", ref)


async def team_domain_ids(conn: asyncpg.Connection, team: UUID) -> list[UUID]:
    """Domains a team belongs to, ordered by slug (the first one is the team's primary domain)."""
    rows = await conn.fetch(
        "SELECT td.domain_id FROM team_domains td JOIN domains d ON d.id = td.domain_id "
        "WHERE td.team_id = $1 ORDER BY d.slug",
        team,
    )
    return [r["domain_id"] for r in rows]
