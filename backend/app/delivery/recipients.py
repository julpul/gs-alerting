"""Recipient resolution (spec 9.1): `oncall`, `user`, `team`, `role`, plus `domain_root` / `master` for
escalation levels. Teams, domains and users may be referenced by slug / email or by uuid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from app.core.ids import ORG_ID, parse_uuid
from app.delivery.ruleview import RecipientSpec
from app.escalation.oncall import current_oncall

log = logging.getLogger(__name__)

USER_COLUMNS = "u.id, u.email, u.display_name, u.phone"


@dataclass(frozen=True)
class Person:
    id: UUID
    email: str
    name: str
    phone: str | None = None


def _person(row: asyncpg.Record) -> Person:
    return Person(id=row["id"], email=row["email"], name=row["display_name"], phone=row["phone"])


async def _team_id(conn: asyncpg.Connection, ref: str) -> UUID | None:
    uid = parse_uuid(ref)
    if uid is not None:
        return await conn.fetchval("SELECT id FROM teams WHERE id = $1", uid)
    return await conn.fetchval("SELECT id FROM teams WHERE slug = $1", ref)


async def _domain_id(conn: asyncpg.Connection, ref: str) -> UUID | None:
    uid = parse_uuid(ref)
    if uid is not None:
        return await conn.fetchval("SELECT id FROM domains WHERE id = $1", uid)
    return await conn.fetchval("SELECT id FROM domains WHERE slug = $1", ref)


async def users_by_ids(conn: asyncpg.Connection, ids: list[UUID]) -> list[Person]:
    if not ids:
        return []
    rows = await conn.fetch(f"SELECT {USER_COLUMNS} FROM users u WHERE u.id = ANY($1)", ids)
    by_id = {r["id"]: _person(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def _team_members(conn: asyncpg.Connection, team_id: UUID) -> list[Person]:
    rows = await conn.fetch(
        f"SELECT {USER_COLUMNS} FROM users u JOIN team_members m ON m.user_id = u.id "
        "WHERE m.team_id = $1 ORDER BY u.email",
        team_id,
    )
    return [_person(r) for r in rows]


async def _role_holders(conn: asyncpg.Connection, role: str, scope_type: str, scope_id: UUID) -> list[Person]:
    rows = await conn.fetch(
        f"SELECT DISTINCT {USER_COLUMNS} FROM users u JOIN role_assignments r ON r.user_id = u.id "
        "WHERE r.role = $1 AND r.scope_type = $2 AND r.scope_id = $3 ORDER BY u.email",
        role,
        scope_type,
        scope_id,
    )
    return [_person(r) for r in rows]


async def domain_roots(conn: asyncpg.Connection, domain_id: UUID) -> list[Person]:
    return await _role_holders(conn, "domain_root", "domain", domain_id)


async def masters(conn: asyncpg.Connection) -> list[Person]:
    return await _role_holders(conn, "master", "org", ORG_ID)


async def _user(conn: asyncpg.Connection, ref: str) -> list[Person]:
    uid = parse_uuid(ref)
    if uid is not None:
        return await users_by_ids(conn, [uid])
    row = await conn.fetchrow(f"SELECT {USER_COLUMNS} FROM users u WHERE lower(u.email) = lower($1)", ref)
    return [_person(row)] if row else []


async def resolve_spec(
    conn: asyncpg.Connection,
    spec: RecipientSpec,
    *,
    team_id: UUID,
    domain_id: UUID,
    at: datetime | None = None,
) -> list[Person]:
    """Resolve one recipient spec to people. Unknown references resolve to nobody (and are logged)."""
    kind, ref = spec.kind, spec.id
    if kind == "user":
        people = await _user(conn, ref)
    elif kind in ("oncall", "team"):
        target = await _team_id(conn, ref) if ref else team_id
        if target is None:
            people = []
        elif kind == "oncall":
            people = await users_by_ids(conn, await current_oncall(conn, target, at))
        else:
            people = await _team_members(conn, target)
    elif kind == "role":
        team_ref, _, role = ref.rpartition(":")
        target = await _team_id(conn, team_ref) if team_ref else team_id
        people = await _role_holders(conn, role, "team", target) if target else []
        if role == "member" and target:
            seen = {p.id for p in people}
            people += [p for p in await _team_members(conn, target) if p.id not in seen]
    elif kind == "domain_root":
        target = (await _domain_id(conn, ref)) if ref else domain_id
        people = await domain_roots(conn, target) if target else []
    elif kind == "master":
        people = await masters(conn)
    else:
        people = []
    if not people:
        log.info("recipient %s:%s resolved to nobody", kind, ref)
    return people


async def resolve_specs(
    conn: asyncpg.Connection,
    specs: list[RecipientSpec],
    *,
    team_id: UUID,
    domain_id: UUID,
    at: datetime | None = None,
) -> list[Person]:
    """Resolve and de-duplicate (first occurrence wins, order preserved)."""
    out: dict[UUID, Person] = {}
    for spec in specs:
        for person in await resolve_spec(conn, spec, team_id=team_id, domain_id=domain_id, at=at):
            out.setdefault(person.id, person)
    return list(out.values())
