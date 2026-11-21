"""On-call resolution (spec 10.3): shift precedence override > manual > rotation, else the whole team."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

KIND_PRECEDENCE = ("override", "manual", "rotation")


async def oncall_from_shifts(
    conn: asyncpg.Connection, team_id: UUID, at: datetime | None = None
) -> list[UUID]:
    """User ids whose shifts cover `at` (default now), highest-precedence kind only; [] without shifts."""
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
    return []


async def current_oncall(conn: asyncpg.Connection, team_id: UUID, at: datetime | None = None) -> list[UUID]:
    """User ids on call for `team_id` at `at` (default now).

    Among the shifts covering `at`, only those of the highest-precedence kind count. Without any shift
    the fallback is every member of the team.
    """
    users = await oncall_from_shifts(conn, team_id, at)
    if users:
        return users
    members = await conn.fetch(
        "SELECT user_id FROM team_members WHERE team_id = $1 ORDER BY user_id", team_id
    )
    return [r["user_id"] for r in members]


NEXT_LOOKAHEAD = timedelta(days=30)
SHIFT_SELECT = (
    "SELECT s.id, s.team_id, s.user_id, s.start_at, s.end_at, s.kind, s.rotation_id, s.created_by, "
    "u.display_name AS user_name, c.display_name AS creator_name "
    "FROM shifts s JOIN users u ON u.id = s.user_id LEFT JOIN users c ON c.id = s.created_by"
)


def winning_shifts(shifts: Sequence[Mapping[str, Any]], at: datetime) -> list[Mapping[str, Any]]:
    """Shifts covering `at` of the highest-precedence kind (override > manual > rotation)."""
    covering = [s for s in shifts if s["start_at"] <= at < s["end_at"]]
    for kind in KIND_PRECEDENCE:
        of_kind = [s for s in covering if s["kind"] == kind]
        if of_kind:
            return of_kind
    return []


def next_change(
    shifts: Sequence[Mapping[str, Any]], at: datetime
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """(shift on call at `at`, first shift after `at` that puts different people on call)."""
    current = winning_shifts(shifts, at)
    current_users = {s["user_id"] for s in current}
    boundaries = sorted(
        {s["start_at"] for s in shifts if s["start_at"] > at}
        | {s["end_at"] for s in shifts if s["end_at"] > at}
    )
    for moment in boundaries:
        upcoming = winning_shifts(shifts, moment)
        if upcoming and {s["user_id"] for s in upcoming} != current_users:
            return (current[0] if current else None), upcoming[0]
    return (current[0] if current else None), None


async def oncall_now_next(
    conn: asyncpg.Connection, team_id: UUID, at: datetime | None = None
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """The shift on call now and the next shift that changes who is on call (rows incl. `user_name`)."""
    at = at or datetime.now(UTC)
    rows = await conn.fetch(
        f"{SHIFT_SELECT} WHERE s.team_id = $1 AND s.end_at > $2 AND s.start_at < $3 "
        "ORDER BY s.start_at, s.user_id",
        team_id,
        at,
        at + NEXT_LOOKAHEAD,
    )
    return next_change([dict(r) for r in rows], at)
