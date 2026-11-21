"""Herald UI: `/api/platforms` (our sources with open-problem counts), `PUT /api/platforms/{id}` (connect a
webhook platform; the key is returned once as `token`), `/api/sources`."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from app.api.deps import Pool, RedisDep
from app.config import get_settings
from app.core.audit import audit
from app.core.errors import AppError, forbidden
from app.herald.codebase import CodebaseIn, ObservabilityIn
from app.herald.identity import Actor
from app.herald.platforms import (
    SOURCE_SELECT,
    alert_counts,
    ingest_path,
    platform_source,
    platform_wire,
    platform_with_counts,
    source_by_name,
    visible_sources,
)
from app.herald.routing import herald_router
from app.repositories import sources as repo
from app.repositories.rules import can_edit_team, resolve_team
from app.schemas.internal import User

router = herald_router()
PLATFORM_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")


@router.get("/api/sources")
async def sources(who: Actor, pool: Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        return [platform_wire(row) for row in await visible_sources(conn, who.scope)]


@router.get("/api/platforms")
async def platforms(who: Actor, pool: Pool, redis: RedisDep) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await visible_sources(conn, who.scope)
        counts = await alert_counts(conn, who.scope)
    return [await platform_with_counts(row, counts, redis) for row in rows]


class PlatformIn(BaseModel):
    name: str | None = None
    codebase: CodebaseIn | None = None  # null removes it
    observability: ObservabilityIn | None = None
    icon: str | None = None
    team: str | None = None  # team slug; default: the "acting for" team, else the caller's own team


async def _team_for(conn: Any, who: Any, ref: str | None) -> Any:
    candidates = [ref] if ref else list(who.teams)
    for candidate in candidates:
        team_id = await resolve_team(conn, candidate.removeprefix("team:"))
        if team_id is not None and await can_edit_team(conn, who.scope, team_id):
            return team_id
    if ref:
        raise forbidden("you cannot connect platforms for that team")
    own = await conn.fetch(
        "SELECT t.id FROM team_members tm JOIN teams t ON t.id = tm.team_id "
        "WHERE tm.user_id = $1 ORDER BY t.slug",
        who.user_id,
    )
    if own:
        return own[0]["id"]
    for team in await conn.fetch("SELECT id FROM teams ORDER BY slug"):
        if await can_edit_team(conn, who.scope, team["id"]):
            return team["id"]
    raise forbidden("you have no team to connect a platform for")


@router.put("/api/platforms/{platform_id}")
async def connect_platform(platform_id: str, body: PlatformIn, who: Actor, pool: Pool) -> dict[str, Any]:
    """Create a webhook source named `platform_id`; its key is returned once (`token`), only a hash kept."""
    if not PLATFORM_ID.fullmatch(platform_id):
        raise AppError(
            422, "validation_error", "use lower-case letters, digits and dashes for the platform id"
        )
    wanted = body.model_fields_set & {"codebase", "observability"}
    async with pool.acquire() as conn:
        existing = await source_by_name(conn, platform_id)
        if existing is not None and not wanted:
            raise AppError(409, "conflict", "a platform with this id is already connected")
        if existing is not None:
            return await _update_links(conn, who, body, wanted, platform_id)
        if body.name is None:
            raise AppError(422, "validation_error", "name: required")
        display = body.name.strip()[:60] or platform_id
        team_id = await _team_for(conn, who, body.team)
        domain_id = await repo.team_domain(conn, team_id)
        if domain_id is None:
            raise AppError(422, "validation_error", "that team has no domain")
        async with conn.transaction():
            row, key = await repo.create_source(
                conn,
                name=platform_id,
                team_id=team_id,
                domain_id=domain_id,
                type_="webhook",
                preset=None,
                config={"herald": {"name": display, "icon": body.icon or "webhook"}},
                mapping=None,
                user=User(**who.user),
            )
        row = await _save_links(conn, row["id"], body, wanted)
    base = get_settings().public_base_url.rstrip("/")
    return {
        **platform_wire(row),
        "firing": 0,
        "acked": 0,
        "resolved": 0,
        "last_event_at": None,
        "token": key,
        "url": f"{base}{ingest_path(platform_id)}",
        "ingestUrl": repo.ingest_url(key),
    }


async def _save_links(conn: Any, source_id: Any, body: PlatformIn, wanted: set[str]) -> Any:
    for column in sorted(wanted):  # column names come from the fixed set above, never from input
        value = getattr(body, column)
        await conn.execute(
            f"UPDATE sources SET {column} = $2 WHERE id = $1",
            source_id,
            value.model_dump() if value is not None else None,
        )
    return await conn.fetchrow(f"{SOURCE_SELECT} WHERE id = $1", source_id)


async def _update_links(
    conn: Any, who: Any, body: PlatformIn, wanted: set[str], platform_id: str
) -> dict[str, Any]:
    existing = await platform_source(conn, who.scope, platform_id)
    if existing is None:
        raise AppError(404, "not_found", "no such platform")
    if not await can_edit_team(conn, who.scope, existing["team_id"]):
        raise forbidden("you cannot edit this platform")
    async with conn.transaction():
        row = await _save_links(conn, existing["id"], body, wanted)
        await audit(
            conn, who.actor, "source.links_updated", "source", str(row["id"]), {"fields": sorted(wanted)}
        )
    return platform_wire(row)
