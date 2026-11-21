"""On-call resolution (spec 10.3): shift precedence override > manual > rotation, else the whole team."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import asyncpg

KIND_PRECEDENCE = ("override", "manual", "rotation")


async def current_oncall(conn: asyncpg.Connection, team_id: UUID, at: datetime | None = None) -> list[UUID]:
    """User ids on call for `team_id` at `at` (default now).

    Among the shifts covering `at`, only those of the highest-precedence kind count. Without any shift
    the fallback is every member of the team.
    """
    at = at or datetime.now(UTC)
    rows = await conn.fetch(
        "SELECT user_id, kind FROM shifts WHERE team_id = $1 AND start_at <= $2 AND end_at > $2 "
        "ORDER BY start_at, user_id",
        team_id,
        at,
    )
    for kind in KIND_PRECEDENCE:
        users = list(dict.fromkeys(r["user_id"] for r in rows if r["kind"] == kind))
        if users:
            return users
    members = await conn.fetch(
        "SELECT user_id FROM team_members WHERE team_id = $1 ORDER BY user_id", team_id
    )
    return [r["user_id"] for r in members]
