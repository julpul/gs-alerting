"""Herald ids <-> our ids (see README.md, "Ids").

alert = `alerts.short_id` (int), person = e-mail local part (`person:anna`), team = slug (`team:team-payments`
as an owner), platform = source name, approval = `approvals.short_id`, step = the step's 1-based `order`.
Statuses: ours `open|escalated` -> `firing`, `acknowledged` -> `acked`, `resolved` -> `resolved`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

import asyncpg

from app.core.errors import AppError, forbidden
from app.core.scope import Scope
from app.herald.identity import person_id

TO_HERALD = {"open": "firing", "escalated": "firing", "acknowledged": "acked", "resolved": "resolved"}
FROM_HERALD = {"firing": ["open", "escalated"], "acked": ["acknowledged"], "resolved": ["resolved"]}
ALL_STATUSES = ["open", "escalated", "acknowledged", "resolved"]


def herald_status(status: str) -> str:
    return TO_HERALD.get(status, "firing")


def our_statuses(status: str | None) -> list[str]:
    """Herald `?status=` (comma list allowed) -> our statuses; none -> every status (Herald lists all)."""
    if not status:
        return list(ALL_STATUSES)
    out: list[str] = []
    for part in (p.strip() for p in status.split(",")):
        if part not in FROM_HERALD:
            raise AppError(422, "validation_error", f"status must be one of {', '.join(FROM_HERALD)}")
        out += FROM_HERALD[part]
    return out


def owner_ref(slug: str) -> str:
    return f"team:{slug}"


def short_from_ref(value: str | int) -> int | None:
    """`17`, `A-17` or `AL-0017` -> 17."""
    text = str(value).strip()
    for prefix in ("AL-", "A-", "al-", "a-"):
        text = text.removeprefix(prefix)
    return int(text) if text.isdigit() else None


async def people_refs(conn: asyncpg.Connection, user_ids: Iterable[Any]) -> dict[UUID, str]:
    """user uuid -> `person:<local part>` for every known id (unknown ids are left out)."""
    ids = sorted({u if isinstance(u, UUID) else _uuid(u) for u in user_ids if u} - {None}, key=str)
    if not ids:
        return {}
    rows = await conn.fetch("SELECT id, email FROM users WHERE id = ANY($1)", ids)
    return {r["id"]: f"person:{person_id(r['email'])}" for r in rows}


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def actor_ref(actor: dict[str, Any] | None, refs: dict[UUID, str]) -> str:
    """Stored actor dict -> `person:x` for people, `agent:<name>` / `system:<name>` otherwise."""
    actor = actor or {}
    kind = actor.get("type") or actor.get("kind") or "system"
    name = actor.get("name") or actor.get("displayName")
    uid = _uuid(actor.get("userId") if kind == "agent" else actor.get("id"))
    if kind == "user" and uid in refs:
        return refs[uid]
    if kind == "agent":
        return f"agent:{name or 'agent'}"
    return f"system:{name or 'herald'}"


def actor_user_ids(actors: Iterable[dict[str, Any] | None]) -> set[UUID]:
    out: set[UUID] = set()
    for actor in actors:
        actor = actor or {}
        uid = _uuid(actor.get("userId") or actor.get("id"))
        if uid:
            out.add(uid)
    return out


async def team_slugs(conn: asyncpg.Connection) -> dict[UUID, str]:
    return {r["id"]: r["slug"] for r in await conn.fetch("SELECT id, slug FROM teams")}


async def team_for_owner(conn: asyncpg.Connection, owner: str, scope: Scope) -> UUID:
    """`team:<slug>` (or a bare slug) the caller may see -> team uuid; 403 like Herald otherwise."""
    slug = owner.removeprefix("team:")
    team_id = await conn.fetchval("SELECT id FROM teams WHERE slug = $1", slug)
    if team_id is None or not scope.can_see_team(team_id):
        raise forbidden("not your team")
    return team_id


async def user_for_person(conn: asyncpg.Connection, ref: str) -> asyncpg.Record | None:
    pid = ref.removeprefix("person:")
    return await conn.fetchrow(
        "SELECT id, email, display_name FROM users WHERE split_part(email, '@', 1) = $1", pid
    )


async def source_id_by_name(conn: asyncpg.Connection, name: str) -> UUID | None:
    return await conn.fetchval("SELECT id FROM sources WHERE name = $1 ORDER BY created_at LIMIT 1", name)
