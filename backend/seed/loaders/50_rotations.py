"""Seed a weekly on-call rotation for every team with members and materialise its first shifts.

Member order and start come from `org.yaml` `rotations` (first member on call now, `startsAt` relative to
the seed day); members missing there follow in e-mail order. A rotation that still has the e-mail order of
earlier seeds (which put e.g. Agnieszka instead of Anna on call for Team Payments) is moved to the fixture
order once, and its current and future shifts are rebuilt. Rotations edited since are never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.core.ids import stable_id
from app.escalation.rotations import materialise_all
from seed.loaders._support import parse_offset


def ordered_members(emails: list[str], preferred: list[str]) -> list[str]:
    """`preferred` first (only those in `emails`), then the rest in e-mail order."""
    first = [e for e in preferred if e in emails]
    return first + sorted(e for e in emails if e not in first)


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    template = fixtures["rotations"]["rotation"]
    plans = {r["team"]: r for r in (fixtures.get("org") or {}).get("rotations") or []}
    seed_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    teams = await conn.fetch(
        "SELECT t.id, t.slug, array_agg(u.email ORDER BY u.email) AS emails "
        "FROM teams t JOIN team_members m ON m.team_id = t.id JOIN users u ON u.id = m.user_id "
        "GROUP BY t.id, t.slug ORDER BY t.slug"
    )
    for team in teams:
        plan = plans.get(team["slug"]) or {}
        emails = list(team["emails"])
        members = [stable_id("user", e) for e in ordered_members(emails, plan.get("members") or [])]
        legacy = [stable_id("user", e) for e in sorted(emails)]
        starts_at = seed_day + parse_offset(plan["startsAt"]) if plan.get("startsAt") else seed_day
        rotation_id = stable_id("rotation", team["slug"])
        await conn.execute(
            "INSERT INTO rotations (id, team_id, name, member_ids, starts_at, shift_hours, timezone) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (id) DO NOTHING",
            rotation_id,
            team["id"],
            template["name"],
            members,
            starts_at,
            template["shiftHours"],
            template["timezone"],
        )
        if members != legacy:
            await _repair_legacy_order(conn, rotation_id, legacy, members, starts_at)
    await materialise_all(conn)


async def _repair_legacy_order(
    conn: asyncpg.Connection, rotation_id: UUID, legacy: list[UUID], members: list[UUID], starts_at: datetime
) -> None:
    moved = await conn.fetchval(
        "UPDATE rotations SET member_ids = $2, starts_at = $3 WHERE id = $1 AND member_ids = $4 RETURNING id",
        rotation_id,
        members,
        starts_at,
        legacy,
    )
    if moved is not None:
        await conn.execute("DELETE FROM shifts WHERE rotation_id = $1 AND end_at > now()", rotation_id)
