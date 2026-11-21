"""Herald UI: `/api/rules` (list, save), `/api/rules/test` (dry-run a condition), `/api/rules/preview`.

Saving goes through the same validation, repository (versions + audit) and hot-reload announcement as
`/v1/rules`. The Herald document is translated by `app.herald.dsl`; what our model cannot hold is kept in
`rules.definition.herald`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from fastapi import Body
from pydantic import BaseModel

from app.api.deps import Pool, RedisDep
from app.api.routers.rules import _announce, _prepare
from app.core.errors import AppError, forbidden
from app.core.ids import parse_uuid
from app.herald.dsl import (
    RULE_SELECT,
    Directory,
    DslError,
    herald_to_rule,
    load_directory,
    rule_to_herald,
    scoped_rules,
    stored_rule,
    when_to_match,
)
from app.herald.explain import attrs, event_view, explain_when, failing_leaves, raw_events
from app.herald.identity import Actor
from app.herald.platforms import platform_source, visible_sources
from app.herald.preview import preview
from app.herald.routing import herald_router
from app.pipeline.fields import EventView
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.model import RuleDef, RuleValidationError, parse_rule
from app.repositories import rules as repo
from app.repositories.sources import read_recent
from app.schemas.internal import User

router = herald_router()
TEST_EVENTS = 50


def invalid(message: str) -> AppError:
    return AppError(422, "invalid_rule", message)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def _stats(conn: asyncpg.Connection, rule_ids: list[UUID]) -> dict[UUID, asyncpg.Record]:
    rows = await conn.fetch(
        "SELECT r.id, (SELECT count(*) FROM alerts a WHERE a.rule_id = r.id) AS alerts, "
        "(SELECT max(last_seen_at) FROM alerts a WHERE a.rule_id = r.id) AS last_fired, "
        "(SELECT v.changed_by FROM rule_versions v WHERE v.rule_id = r.id ORDER BY v.version DESC LIMIT 1) "
        "AS changed_by FROM rules r WHERE r.id = ANY($1)",
        rule_ids,
    )
    return {r["id"]: r for r in rows}


@router.get("/api/rules")
async def list_rules(who: Actor, pool: Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        directory = await load_directory(conn)
        rules = await scoped_rules(conn, who.scope)
        stats = await _stats(conn, [s.rule.id for s in rules])
    out = []
    for item in rules:
        st = stats.get(item.rule.id)
        changed_by = st["changed_by"] if st else None
        out.append(
            {
                "doc": rule_to_herald(item.rule, directory, item.extras),
                "version": item.rule.version,
                "source": "api" if changed_by or item.extras else "seed",
                "updated_at": _iso(item.row["updated_at"]),
                "updated_by": f"person:{directory.person_of(str(changed_by))}" if changed_by else None,
                "alerts": st["alerts"] if st else 0,
                "last_fired": _iso(st["last_fired"]) if st else None,
            }
        )
    return out


async def _existing(conn: asyncpg.Connection, who: Any, rule_id: str) -> asyncpg.Record | None:
    """By uuid, or by the id the UI made up for a new rule (kept as `definition.herald.clientId`)."""
    uid = parse_uuid(rule_id)
    if uid is not None:
        row = await conn.fetchrow(f"{RULE_SELECT} WHERE r.id = $1", uid)
    else:
        row = await conn.fetchrow(f"{RULE_SELECT} WHERE r.definition->'herald'->>'clientId' = $1", rule_id)
    if row is not None and not who.scope.can_see_team(row["team_id"]):
        raise forbidden("that rule belongs to another team")
    return row


@router.put("/api/rules/{rule_id}")
async def save_rule(
    rule_id: str, who: Actor, pool: Pool, redis: RedisDep, doc: dict[str, Any] = Body()
) -> dict[str, Any]:
    user = User(**who.user)
    async with pool.acquire() as conn:
        directory = await load_directory(conn)
        row = await _existing(conn, who, rule_id)
        current = stored_rule(row) if row is not None else None
        try:
            data, extras = herald_to_rule(doc, directory, current.rule if current else None)
        except DslError as exc:
            raise invalid(str(exc)) from exc
        rule = await _prepare(conn, data)
        if not await repo.can_edit_team(conn, who.scope, rule.team_id):
            raise forbidden("you can only save rules for your own teams")
        if row is not None and not await repo.can_edit_team(conn, who.scope, row["team_id"]):
            raise forbidden("that rule belongs to another team")
        if parse_uuid(rule_id) is None:
            extras["clientId"] = rule_id
        elif current is not None and current.extras.get("clientId"):
            extras["clientId"] = current.extras["clientId"]
        async with conn.transaction():
            saved = (
                await repo.update_rule(conn, row, rule, user)
                if row
                else await repo.insert_rule(conn, rule, user)
            )
            await conn.execute(
                "UPDATE rules SET definition = definition || jsonb_build_object('herald', $2::jsonb) "
                "WHERE id = $1",
                saved.id,
                extras,
            )
    await _announce(redis, saved.id, saved.version)
    return {"id": str(saved.id), "version": saved.version}


class RuleTest(BaseModel):
    when: dict[str, Any]
    events: list[Any] | None = None
    source: str | None = None


def _condition(when: dict[str, Any]) -> RuleDef:
    try:
        rule = parse_rule({"name": "test", "teamId": str(UUID(int=0)), "match": when_to_match(when)})
        compile_rule(rule)
    except DslError as exc:
        raise invalid(str(exc)) from exc
    except RuleValidationError as exc:
        raise invalid(f"{exc.path}: {exc.message}".lstrip(": ")) from exc
    return rule


async def _recent(
    conn: asyncpg.Connection, redis: Any, who: Any, source: str | None, limit: int
) -> list[tuple[str, Any, EventView, str | None, dict[str, Any]]]:
    """(source name, received_at, view, event id, raw) for the newest events of the caller's sources."""
    if source:
        row = await platform_source(conn, who.scope, source)
        rows = [row] if row is not None else []
    else:
        rows = await visible_sources(conn, who.scope)
    items = []
    for row in rows:
        for ev in await read_recent(redis, row["id"], limit=limit):
            items.append((row["name"], ev.received_at, EventView(ev.raw, ev.meta), ev.event_id, ev.raw))
    items.sort(key=lambda item: item[1], reverse=True)
    return items[:limit]


@router.post("/api/rules/test")
async def test_rule(body: RuleTest, who: Actor, pool: Pool, redis: RedisDep) -> dict[str, Any]:
    """Matches N of the last M events, and the condition that failed for each miss."""
    _condition(body.when)
    async with pool.acquire() as conn:
        if body.events is None:
            items = await _recent(conn, redis, who, body.source, TEST_EVENTS)
        else:
            src = await platform_source(conn, who.scope, body.source) if body.source else None
            items = [
                (body.source, None, event_view(raw, dict(src) if src else None), None, raw)
                for event in body.events
                for raw in raw_events(event, dict(src) if src else None)
            ]
    results = []
    for source, received_at, ev, event_id, raw in items:
        why = explain_when(body.when, ev)
        results.append(
            {
                "id": event_id,
                "source": source,
                "received_at": _iso(received_at),
                "passed": why["passed"],
                "failed_because": failing_leaves(why),
                "event": raw,
                "attrs": attrs(ev),
            }
        )
    return {"matched": sum(r["passed"] for r in results), "total": len(results), "results": results}


class PreviewIn(BaseModel):
    rule: dict[str, Any]
    event: Any
    source: str | None = None


async def _draft(conn: asyncpg.Connection, doc: dict[str, Any], directory: Directory) -> tuple[RuleDef, dict]:
    try:
        data, extras = herald_to_rule(doc, directory)
    except DslError as exc:
        raise invalid(str(exc)) from exc
    return await _prepare(conn, data), extras


@router.post("/api/rules/preview")
async def preview_rule(body: PreviewIn, who: Actor, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        directory = await load_directory(conn)
        rule, extras = await _draft(conn, body.rule, directory)
        src = await platform_source(conn, who.scope, body.source) if body.source else None
        source = dict(src) if src else None
        raws = raw_events(body.event, source)
        if not raws:
            raise AppError(422, "validation_error", "event: nothing to preview")
        return await preview(
            conn, rule, event_view(raws[0], source), highlight=extras["highlight"], directory=directory
        )
