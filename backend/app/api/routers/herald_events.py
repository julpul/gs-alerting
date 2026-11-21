"""Herald UI: `/api/events/recent`, `/api/events/explain`, `/api/fields`.

Events come from our Redis `recent:<source_id>` sample streams (written by the worker with the normalized
`_meta`). `fired` / `closed` are recomputed with the caller's current rules (the same compiled predicates
the worker uses); `/api/fields` is derived from the same streams, so a never-seen schema shows its fields
as soon as its first event is in.
"""

from __future__ import annotations

from typing import Any

from fastapi import Query
from pydantic import BaseModel

from app.api.deps import Pool, RedisDep
from app.herald.dsl import Directory, StoredRule, load_directory, rule_to_herald, scoped_rules
from app.herald.explain import attrs, closes, event_view, explain_rule, raw_events
from app.herald.identity import Actor
from app.herald.platforms import platform_source, visible_sources
from app.herald.routing import herald_router
from app.pipeline.fields import EventView
from app.pipeline.rules.compiler import CompiledRule, compile_rule
from app.pipeline.rules.model import RuleValidationError
from app.repositories.sources import read_recent

router = herald_router()
FIELD_EVENTS = 50
MAX_PATHS = 200
ALIASES = ("type", "severity", "service", "timestamp", "source", "schema", "environment")


async def _sources(conn: Any, who: Any, source: str | None) -> list[Any]:
    if source:
        row = await platform_source(conn, who.scope, source)
        return [row] if row is not None else []
    return await visible_sources(conn, who.scope)


def _owner(item: StoredRule, directory: Directory) -> str:
    return rule_to_herald(item.rule, directory, item.extras)["owner"]


class _Matcher:
    """The caller's enabled rules, compiled once per request."""

    def __init__(self, rules: list[StoredRule]) -> None:
        self.items: list[tuple[StoredRule, CompiledRule]] = []
        for item in rules:
            if not item.rule.enabled:
                continue
            try:
                self.items.append((item, compile_rule(item.rule)))
            except RuleValidationError:
                continue

    def outcome(self, ev: EventView) -> tuple[list[str], list[str]]:
        closed = [str(item.rule.id) for item, _ in self.items if closes(item.extras.get("resolve_when"), ev)]
        fired = [
            str(item.rule.id) for item, c in self.items if c.matches(ev) and str(item.rule.id) not in closed
        ]
        return fired, closed


@router.get("/api/events/recent")
async def recent_events(
    who: Actor, pool: Pool, redis: RedisDep, source: str | None = None, limit: int = Query(50, ge=1, le=200)
) -> list[dict[str, Any]]:
    """What arrived lately, newest first, with the rules each event fires (or closes) now."""
    async with pool.acquire() as conn:
        rows = await _sources(conn, who, source)
        matcher = _Matcher(await scoped_rules(conn, who.scope))
    items = []
    for row in rows:
        for ev in await read_recent(redis, row["id"], limit=limit):
            items.append((ev, row["name"]))
    items.sort(key=lambda item: item[0].received_at, reverse=True)
    out = []
    for ev, name in items[:limit]:
        view = EventView(ev.raw, ev.meta)
        fired, closed = matcher.outcome(view)
        out.append(
            {
                "id": ev.event_id,
                "source": name,
                "received_at": ev.received_at.isoformat(),
                "raw": ev.raw,
                "attrs": attrs(view),
                "fired": fired,
                "closed": closed,
            }
        )
    return out


class EventIn(BaseModel):
    event: Any
    source: str | None = None


@router.post("/api/events/explain")
async def explain_event(body: EventIn, who: Actor, pool: Pool) -> dict[str, Any]:
    """Every rule the caller can see against one event: matched or not, and the condition that failed."""
    async with pool.acquire() as conn:
        src = await platform_source(conn, who.scope, body.source) if body.source else None
        directory = await load_directory(conn)
        rules = await scoped_rules(conn, who.scope)
    source = dict(src) if src else None
    items = []
    for raw in raw_events(body.event, source) or [{}]:
        ev = event_view(raw, source)
        explained = []
        for item in rules:
            result = explain_rule(item.rule, ev, item.extras.get("resolve_when"))
            explained.append(
                {
                    "id": str(item.rule.id),
                    "name": item.rule.name,
                    "owner": _owner(item, directory),
                    "enabled": item.rule.enabled,
                    **result,
                }
            )
        items.append({"attrs": attrs(ev), "meta": ev.meta, "rules": explained, "event": raw})
    return {"attrs": items[0]["attrs"], "rules": items[0]["rules"], "items": items}


def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if len(out) >= MAX_PATHS:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(child, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            _flatten(child, f"{prefix}.{index}" if prefix else str(index), out)
    elif prefix:
        out[prefix] = value


@router.get("/api/fields")
async def fields(who: Actor, pool: Pool, redis: RedisDep, source: str | None = None) -> list[dict[str, Any]]:
    """Every field path in the caller's recent events, with a type and an example (the field picker)."""
    async with pool.acquire() as conn:
        rows = await _sources(conn, who, source)
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        for ev in reversed(await read_recent(redis, row["id"], limit=FIELD_EVENTS)):
            paths: dict[str, Any] = {}
            _flatten(ev.raw, "", paths)
            for alias in ALIASES:
                if ev.meta.get(alias) is not None:
                    paths[f"@{alias}"] = ev.meta[alias]
            for path, value in paths.items():
                entry = seen.setdefault((row["name"], path), {"source": row["name"], "path": path, "seen": 0})
                entry.update(type=type(value).__name__, example=str(value)[:120])
                entry["seen"] += 1
    return [seen[key] for key in sorted(seen)]
