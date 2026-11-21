"""Rotations, schedules (shifts) and who is on call (spec 10.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from fastapi import APIRouter, Query
from pydantic import AwareDatetime

from app.api.deps import CurrentScope, CurrentUser, Pool
from app.core.audit import audit, user_actor
from app.core.errors import AppError, forbidden, not_found
from app.core.ids import parse_uuid, uuid7
from app.core.scope import Scope
from app.escalation import refs, rotations
from app.escalation.chain import escalation_chain
from app.escalation.oncall import SHIFT_SELECT, oncall_now_next
from app.escalation.policies import effective_policy
from app.schemas import api as wire
from app.schemas.internal import User

router = APIRouter(prefix="/v1", tags=["schedules"])

DEFAULT_PAST = timedelta(days=7)
DEFAULT_AHEAD = timedelta(days=28)
MAX_SHIFT_HOURS = 24 * 31
SHIFT_KINDS = ("manual", "override")


class ShiftOut(wire.Shift):
    userName: str | None = None


class OnCallOut(wire.OnCallNow):
    now: ShiftOut | None = None
    next: ShiftOut | None = None


def _validation(message: str) -> AppError:
    return AppError(422, "validation_error", message)


def shift_out(row: dict[str, Any] | asyncpg.Record) -> dict[str, Any]:
    created_by = None
    if row["created_by"]:
        created_by = {
            "kind": "user",
            "id": str(row["created_by"]),
            "displayName": row["creator_name"] or "unknown",
            "onBehalfOf": None,
        }
    return {
        "id": str(row["id"]),
        "teamId": str(row["team_id"]),
        "userId": str(row["user_id"]),
        "userName": row["user_name"],
        "start": row["start_at"],
        "end": row["end_at"],
        "kind": row["kind"],
        "createdBy": created_by,
    }


def rotation_out(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "teamId": str(row["team_id"]),
        "name": row["name"],
        "memberIds": [str(m) for m in row["member_ids"]],
        "startsAt": row["starts_at"],
        "shiftHours": row["shift_hours"],
        "handoverTime": row["handover_time"],
        "timezone": row["timezone"],
    }


def actor_out(user_id: UUID, name: str) -> dict[str, Any]:
    return {"kind": "user", "id": str(user_id), "displayName": name, "onBehalfOf": None}


async def _visible_team(conn: asyncpg.Connection, scope: Scope, ref: str) -> UUID:
    team = await refs.team_id(conn, ref)
    if team is None or not scope.can_see_team(team):
        raise not_found("team")
    return team


async def _is_team_editor(conn: asyncpg.Connection, scope: Scope, team: UUID) -> bool:
    """Master, or Domain Root of one of the team's domains."""
    if scope.is_master:
        return True
    domains = set(await refs.team_domain_ids(conn, team))
    return any(
        r.role == "domain_root" and r.scope_type == "domain" and r.scope_id in domains for r in scope.roles
    )


async def _user_ids(conn: asyncpg.Connection, raw: list[str], team: UUID, what: str) -> list[UUID]:
    ids = [parse_uuid(v) for v in raw]
    if not ids or any(i is None for i in ids):
        raise _validation(f"{what}: must be a non-empty list of user ids")
    members = {
        r["user_id"] for r in await conn.fetch("SELECT user_id FROM team_members WHERE team_id = $1", team)
    }
    missing = [str(i) for i in ids if i not in members]
    if missing:
        raise _validation(f"{what}: {missing[0]} is not a member of the team")
    return ids  # type: ignore[return-value]


@router.get("/rotations", response_model=list[wire.Rotation])
async def list_rotations(scope: CurrentScope, pool: Pool, teamId: str) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        team = await _visible_team(conn, scope, teamId)
        rows = await conn.fetch("SELECT * FROM rotations WHERE team_id = $1 ORDER BY starts_at, id", team)
    return [rotation_out(r) for r in rows]


@router.post("/rotations", status_code=201, response_model=wire.Rotation)
async def create_rotation(
    body: wire.Rotation, user: CurrentUser, scope: CurrentScope, pool: Pool
) -> dict[str, Any]:
    if not 1 <= body.shiftHours <= MAX_SHIFT_HOURS:
        raise _validation(f"shiftHours: must be between 1 and {MAX_SHIFT_HOURS}")
    try:
        rotations.parse_handover(body.handoverTime)
        ZoneInfo(body.timezone or "Europe/Warsaw")
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise _validation(f"handoverTime/timezone: {exc}") from exc
    async with pool.acquire() as conn, conn.transaction():
        team = await _visible_team(conn, scope, body.teamId)
        if not await _is_team_editor(conn, scope, team):
            raise forbidden("only a Domain Root of the team's domain or a Master may create rotations")
        members = await _user_ids(conn, body.memberIds, team, "memberIds")
        row = await conn.fetchrow(
            "INSERT INTO rotations "
            "(id, team_id, name, member_ids, starts_at, shift_hours, handover_time, timezone) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
            uuid7(),
            team,
            body.name or "Rotation",
            members,
            body.startsAt,
            body.shiftHours,
            body.handoverTime,
            body.timezone or "Europe/Warsaw",
        )
        await rotations.materialise_rotation(conn, dict(row))
        await audit(
            conn, user_actor(user.id, user.display_name), "rotation.create", "rotation", str(row["id"])
        )
    return rotation_out(row)


@router.get("/schedules", response_model=list[ShiftOut])
async def list_shifts(
    scope: CurrentScope,
    pool: Pool,
    teamId: str,
    start: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    end: Annotated[AwareDatetime | None, Query(alias="to")] = None,
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    window_start, window_end = start or now - DEFAULT_PAST, end or now + DEFAULT_AHEAD
    async with pool.acquire() as conn:
        team = await _visible_team(conn, scope, teamId)
        rows = await conn.fetch(
            f"{SHIFT_SELECT} WHERE s.team_id = $1 AND s.end_at > $2 AND s.start_at < $3 "
            "ORDER BY s.start_at, s.user_id",
            team,
            window_start,
            window_end,
        )
    return [shift_out(r) for r in rows]


async def _can_add_shift(
    conn: asyncpg.Connection, scope: Scope, actor: User, team: UUID, target: UUID
) -> bool:
    if actor.id == target or await _is_team_editor(conn, scope, team):
        return True
    manager = await conn.fetchval("SELECT manager_id FROM users WHERE id = $1", target)
    return manager == actor.id


@router.post("/schedules", status_code=201, response_model=ShiftOut)
async def create_shift(
    body: wire.Shift, user: CurrentUser, scope: CurrentScope, pool: Pool
) -> dict[str, Any]:
    if body.teamId is None or body.userId is None or body.start is None or body.end is None:
        raise _validation("teamId, userId, start and end are required")
    kind = body.kind.value if body.kind else "manual"
    if kind not in SHIFT_KINDS:
        raise _validation("kind: must be manual or override (rotation shifts are generated)")
    if body.end <= body.start:
        raise _validation("end: must be after start")
    target = parse_uuid(body.userId)
    if target is None:
        raise _validation("userId: must be a uuid")
    async with pool.acquire() as conn, conn.transaction():
        team = await _visible_team(conn, scope, body.teamId)
        await _user_ids(conn, [body.userId], team, "userId")
        if not await _can_add_shift(conn, scope, user, team, target):
            raise forbidden("you can only add shifts for yourself, your reports, or as Domain Root / Master")
        shift_id = uuid7()
        await conn.execute(
            "INSERT INTO shifts (id, team_id, user_id, start_at, end_at, kind, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            shift_id,
            team,
            target,
            body.start,
            body.end,
            kind,
            user.id,
        )
        await audit(conn, user_actor(user.id, user.display_name), "shift.create", "shift", str(shift_id))
        row = await conn.fetchrow(f"{SHIFT_SELECT} WHERE s.id = $1", shift_id)
    return shift_out(row)


@router.get("/teams/{team_id}/oncall", response_model=OnCallOut)
async def oncall(team_id: str, scope: CurrentScope, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        team = await _visible_team(conn, scope, team_id)
        now_shift, next_shift = await oncall_now_next(conn, team)
        domains = await refs.team_domain_ids(conn, team)
        chain: list[dict[str, Any]] = []
        if domains:
            policy = await effective_policy(
                conn, rule=None, team_id=team, domain_id=domains[0], severity="critical"
            )
            seen: set[UUID] = set()
            for level in await escalation_chain(conn, policy, team_id=team, domain_id=domains[0]):
                for person in level.people:
                    if person.id not in seen:
                        seen.add(person.id)
                        chain.append(actor_out(person.id, person.name))
    return {
        "teamId": str(team),
        "now": shift_out(now_shift) if now_shift else None,
        "next": shift_out(next_shift) if next_shift else None,
        "escalationChain": chain,
    }
