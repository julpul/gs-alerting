"""Seed a weekly on-call rotation for every team with members and materialise its first shifts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.core.ids import stable_id
from app.escalation.rotations import materialise_all


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    template = fixtures["rotations"]["rotation"]
    starts_at = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    teams = await conn.fetch(
        "SELECT t.id, t.slug, array_agg(m.user_id ORDER BY u.email) AS members "
        "FROM teams t JOIN team_members m ON m.team_id = t.id JOIN users u ON u.id = m.user_id "
        "GROUP BY t.id, t.slug ORDER BY t.slug"
    )
    for team in teams:
        await conn.execute(
            "INSERT INTO rotations (id, team_id, name, member_ids, starts_at, shift_hours, timezone) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (id) DO NOTHING",
            stable_id("rotation", team["slug"]),
            team["id"],
            template["name"],
            team["members"],
            starts_at,
            template["shiftHours"],
            template["timezone"],
        )
    await materialise_all(conn)
