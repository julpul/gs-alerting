"""Dry-run (spec 7.5): evaluate a rule against the `recent:{source_id}` streams of its team's sources.

"After dedup" counts distinct dedup keys: the values of `dedup.fields`, else the rendered title. With
`grouping.byField` set, events sharing that field value collapse into one alert (window ignored).
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any
from uuid import UUID

import asyncpg
import redis.asyncio as aioredis

from app.pipeline.fields import MISSING, EventView
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.model import Recipient, RuleDef
from app.pipeline.rules.templates import render, stringify
from app.repositories.rules import actor_wire, resolve_team
from app.repositories.sources import read_recent

MAX_EVENTS = 5000
MAX_SAMPLES = 10


def _dedup_key(rule: RuleDef, ev: EventView) -> str:
    if rule.grouping and rule.grouping.by_field:
        value = ev.lookup(rule.grouping.by_field)
        if value is not MISSING:
            return "g:" + stringify(value)
    if rule.dedup.fields:
        return "d:" + "\x1f".join(stringify(ev.lookup(f)) for f in rule.dedup.fields)
    return "t:" + render(rule.presentation.title_template or rule.name, ev)


async def _team_members(conn: asyncpg.Connection, team_id: UUID) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT u.id, u.display_name FROM team_members tm JOIN users u ON u.id = tm.user_id "
        "WHERE tm.team_id = $1 ORDER BY u.display_name",
        team_id,
    )


async def resolve_recipients(
    conn: asyncpg.Connection, recipients: Iterable[Recipient], rule_team_id: UUID
) -> list[dict[str, Any]]:
    """Recipients -> distinct user actors (user ids/emails, team slugs/uuids, `team:role` ids)."""
    users: dict[UUID, str] = {}
    for rec in recipients:
        ref = rec.id
        if rec.kind == "user":
            row = await conn.fetchrow(
                "SELECT id, display_name FROM users WHERE lower(email) = lower($1) OR id::text = $1", ref
            )
            rows = [row] if row else []
        elif rec.kind == "role":
            team_ref, _, role = ref.partition(":")
            team_id = await resolve_team(conn, team_ref) if role else rule_team_id
            role = role or team_ref
            rows = await conn.fetch(
                "SELECT u.id, u.display_name FROM role_assignments ra JOIN users u ON u.id = ra.user_id "
                "WHERE ra.role = $1 AND ra.scope_type = 'team' AND ra.scope_id = $2",
                role,
                team_id,
            )
        else:
            team_id = await resolve_team(conn, ref)
            if team_id is None:
                continue
            rows = (
                await _oncall_or_members(conn, team_id)
                if rec.kind == "oncall"
                else await _team_members(conn, team_id)
            )
        for row in rows:
            users[row["id"]] = row["display_name"]
    return [actor_wire(uid, name) for uid, name in users.items()]


async def _oncall_or_members(conn: asyncpg.Connection, team_id: UUID) -> list[asyncpg.Record]:
    try:
        from app.escalation.oncall import current_oncall
    except ImportError:
        return await _team_members(conn, team_id)
    user_ids = await current_oncall(conn, team_id)
    return await conn.fetch("SELECT id, display_name FROM users WHERE id = ANY($1)", list(user_ids))


async def dry_run(
    conn: asyncpg.Connection,
    redis: aioredis.Redis,
    rule: RuleDef,
    *,
    lookback_minutes: int = 60,
) -> dict[str, Any]:
    """The `DryRunResult` JSON for `rule` (raises `RuleValidationError` when it does not compile)."""
    compiled = compile_rule(rule)
    since_ms = int((time.time() - max(lookback_minutes, 1) * 60) * 1000)
    source_ids = [
        r["id"] for r in await conn.fetch("SELECT id FROM sources WHERE team_id = $1", rule.team_id)
    ]

    evaluated = 0
    matched = 0
    keys: set[str] = set()
    samples: list[dict[str, Any]] = []
    for source_id in source_ids:
        remaining = MAX_EVENTS - evaluated
        if remaining <= 0:
            break
        for event in await read_recent(redis, source_id, limit=remaining, since_ms=since_ms):
            evaluated += 1
            view = EventView(event.raw, event.meta)
            if not compiled.matches(view):
                continue
            matched += 1
            keys.add(_dedup_key(rule, view))
            if len(samples) < MAX_SAMPLES:
                samples.append(
                    {"eventId": event.event_id, "receivedAt": event.received_at, "payload": event.raw}
                )

    notifications: list[dict[str, Any]] = []
    if keys:
        for kind, recipients in (("hard", rule.routing.hard), ("soft", rule.routing.soft)):
            for actor in await resolve_recipients(conn, recipients, rule.team_id):
                notifications.append({"recipient": actor, "kind": kind, "count": len(keys)})
    return {
        "evaluated": evaluated,
        "matched": matched,
        "alertsAfterDedup": len(keys),
        "sampleMatches": samples,
        "notifications": notifications,
    }
