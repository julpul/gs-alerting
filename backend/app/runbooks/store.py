"""Runbook storage: list (scoped), create from steps or from pasted content, seed upsert."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from app.core.audit import audit, user_actor
from app.core.errors import AppError, forbidden
from app.core.ids import parse_uuid, uuid7
from app.core.scope import Scope
from app.runbooks.actions.registry import get_action
from app.runbooks.model import ParsedRunbook, finalize, from_stored, to_stored
from app.runbooks.parsers import parse_runbook
from app.schemas import api as wire
from app.schemas.internal import User


def split_attached(attached: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """`runbooks.attached` also carries `parsedBy`; returns (attachment rules, parsedBy)."""
    meta = dict(attached or {})
    return meta, meta.pop("parsedBy", None)


def runbook_out(row: asyncpg.Record) -> dict[str, Any]:
    steps = [from_stored(s).model_dump(mode="json") for s in row["steps"] or []]
    attached, parsed_by = split_attached(row["attached"])
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "format": row["format"],
        "source": row["source"],
        "steps": steps,
        "teamId": str(row["team_id"]) if row["team_id"] else None,
        "attached": attached,
        "parsedBy": parsed_by,
    }


async def list_runbooks(conn: asyncpg.Connection, scope: Scope) -> list[dict[str, Any]]:
    if scope.is_master:
        rows = await conn.fetch("SELECT * FROM runbooks ORDER BY name")
    else:
        rows = await conn.fetch(
            "SELECT * FROM runbooks WHERE team_id IS NULL OR team_id = ANY($1) ORDER BY name",
            list(scope.team_ids),
        )
    return [runbook_out(r) for r in rows]


async def _team_id(conn: asyncpg.Connection, ref: str) -> UUID | None:
    uid = parse_uuid(ref)
    if uid is not None:
        return await conn.fetchval("SELECT id FROM teams WHERE id = $1", uid)
    return await conn.fetchval("SELECT id FROM teams WHERE slug = $1", ref)


async def _require_team_role(conn: asyncpg.Connection, scope: Scope, team_id: UUID) -> None:
    if scope.is_master:
        return
    rows = await conn.fetch("SELECT domain_id FROM team_domains WHERE team_id = $1", team_id)
    domains = {r["domain_id"] for r in rows}
    for r in scope.roles:
        if r.role == "member" and r.scope_type == "team" and r.scope_id == team_id:
            return
        if r.role == "domain_root" and r.scope_type == "domain" and r.scope_id in domains:
            return
    raise forbidden("you need the member role (or higher) on the team to add runbooks")


async def _owner_team(conn: asyncpg.Connection, scope: Scope, ref: str | None) -> UUID | None:
    if ref:
        team_id = await _team_id(conn, ref)
        if team_id is None:
            raise AppError(422, "validation_error", "teamId: team not found")
    else:
        own = [r.scope_id for r in scope.roles if r.role == "member" and r.scope_type == "team"]
        if len(own) == 1:
            team_id = own[0]
        elif scope.is_master:
            return None
        else:
            raise AppError(422, "validation_error", "teamId: required (you belong to several teams)")
    await _require_team_role(conn, scope, team_id)
    return team_id


def _check_actions(steps: list[wire.RunbookStep]) -> None:
    for step in steps:
        if step.action and get_action(step.action) is None:
            raise AppError(422, "validation_error", f"steps.{step.id}.action: unknown action {step.action!r}")


async def create_runbook(
    conn: asyncpg.Connection,
    scope: Scope,
    user: User,
    *,
    name: str | None,
    fmt: str | None,
    source: str | None,
    steps: list[wire.RunbookStep] | None,
    team_ref: str | None,
    attached: dict[str, Any] | None,
) -> dict[str, Any]:
    if not steps and not (source or "").strip():
        raise AppError(422, "validation_error", "source: provide the runbook content or steps")
    team_id = await _owner_team(conn, scope, team_ref)
    parsed: ParsedRunbook | None = None
    if steps:
        final = finalize(steps)
    else:
        parsed = await parse_runbook(source or "", fmt or "auto")
        final = parsed.steps
    _check_actions(final)
    meta = {**(parsed.attached if parsed else {}), **(attached or {})}
    parsed_by = parsed.parsed_by if parsed else "deterministic"
    if parsed_by != "deterministic":
        meta["parsedBy"] = parsed_by
    runbook_id = uuid7()
    async with conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO runbooks (id, name, format, source, steps, attached, team_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
            runbook_id,
            (name or (parsed.name if parsed else None) or "Runbook")[:200],
            fmt if fmt in ("yaml", "markdown", "text", "link") else (parsed.format if parsed else "yaml"),
            source,
            [to_stored(s) for s in final],
            meta,
            team_id,
        )
        await audit(
            conn, user_actor(user.id, user.display_name), "runbook.create", "runbook", str(runbook_id)
        )
    return runbook_out(row)


async def upsert_seed(
    conn: asyncpg.Connection,
    runbook_id: UUID,
    parsed: ParsedRunbook,
    *,
    name: str,
    source: str,
    team_id: UUID | None,
    attached: dict[str, Any],
) -> None:
    await conn.execute(
        "INSERT INTO runbooks (id, name, format, source, steps, attached, team_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, format = EXCLUDED.format, "
        "source = EXCLUDED.source, steps = EXCLUDED.steps, attached = EXCLUDED.attached, "
        "team_id = EXCLUDED.team_id",
        runbook_id,
        name,
        parsed.format,
        source,
        [to_stored(s) for s in parsed.steps],
        {**attached, "parsedBy": parsed.parsed_by} if parsed.parsed_by != "deterministic" else attached,
        team_id,
    )
