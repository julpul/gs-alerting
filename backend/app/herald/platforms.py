"""Our sources as Herald `Platform`s. Platform id = source **name** (unique among visible sources).

Herald's demo UI and lab still name a few platforms by their Herald ids; `PLATFORM_ALIASES` maps those to
our seeded sources so the built-in "send an example" buttons work unchanged. The display name and icon a
platform got in the UI live in `sources.config.herald` (webhook sources have no other config).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg
import redis.asyncio as aioredis

from app.config import get_settings
from app.core.scope import Scope
from app.repositories.sources import stream_stats

SOURCE_SELECT = (
    "SELECT id, name, team_id, domain_id, type, preset, key_prefix, config, mapping, mapping_status, "
    "default_severity, created_at, codebase, observability FROM sources"
)
PLATFORM_ALIASES = {
    "workflow-events": "workflow-broker",
    "metrics-webhook": "prometheus",
    "trade-events": "trade-gateway",
    "generic-webhook": "custody",
}
DEFAULT_ICONS = {
    "workflow-broker": "workflow",
    "prometheus": "monitoring",
    "alertmanager": "monitoring",
    "trade-gateway": "trade",
    "pricing-logs": "logs",
}
# Herald platform types the UI knows: webhook | kafka | logtail | http_poll. RabbitMQ is "a message broker".
TYPE_MAP = {"webhook": "webhook", "rabbitmq": "kafka"}


def herald_meta(row: asyncpg.Record) -> dict[str, Any]:
    config = row["config"] or {}
    meta = config.get("herald") if isinstance(config, dict) else None
    return meta if isinstance(meta, dict) else {}


def ingest_path(name: str) -> str:
    return f"/ingest/{name}"


def platform_wire(row: asyncpg.Record) -> dict[str, Any]:
    """The Herald `Platform` fields that do not need counts (also what `/api/sources` returns)."""
    meta = herald_meta(row)
    config = row["config"] if isinstance(row["config"], dict) else {}
    kind = row["type"]
    return {
        "id": row["name"],
        "name": meta.get("name") or row["name"],
        "icon": meta.get("icon")
        or DEFAULT_ICONS.get(row["name"])
        or DEFAULT_ICONS.get(row["preset"] or "")
        or "webhook",
        "type": TYPE_MAP.get(kind, kind),
        "broker": kind if kind == "rabbitmq" else None,
        "topic": config.get("queue") if kind == "rabbitmq" else None,
        "path": ingest_path(row["name"]) if kind == "webhook" else None,
        "url": f"{get_settings().public_base_url.rstrip('/')}{ingest_path(row['name'])}"
        if kind == "webhook"
        else None,
        "managed": "ui" if meta else "file",
        "needs_token": True,
        "team": str(row["team_id"]),
        "codebase": row["codebase"],
        "observability": row["observability"],
    }


async def visible_sources(conn: asyncpg.Connection, scope: Scope) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"{SOURCE_SELECT} WHERE team_id = ANY($1) ORDER BY name, id",
        [t for t in scope.team_ids if scope.can_see_team(t)],
    )


async def source_by_name(conn: asyncpg.Connection, name: str) -> asyncpg.Record | None:
    """Unscoped lookup (ingest alias authenticates by key instead). Aliases resolve to our seeded names."""
    for candidate in dict.fromkeys([name, PLATFORM_ALIASES.get(name)]):
        if candidate:
            row = await conn.fetchrow(
                f"{SOURCE_SELECT} WHERE name = $1 ORDER BY created_at LIMIT 1", candidate
            )
            if row is not None:
                return row
    return None


async def platform_source(conn: asyncpg.Connection, scope: Scope, name: str) -> asyncpg.Record | None:
    row = await source_by_name(conn, name)
    if row is None or not scope.can_see_team(row["team_id"]):
        return None
    return row


async def alert_counts(conn: asyncpg.Connection, scope: Scope) -> dict[Any, asyncpg.Record]:
    rows = await conn.fetch(
        "SELECT source_id, count(*) FILTER (WHERE status IN ('open', 'escalated')) AS firing, "
        "count(*) FILTER (WHERE status = 'acknowledged') AS acked, "
        "count(*) FILTER (WHERE status = 'resolved') AS resolved "
        "FROM alerts WHERE team_id = ANY($1) AND source_id IS NOT NULL GROUP BY source_id",
        [t for t in scope.team_ids if scope.can_see_team(t)],
    )
    return {r["source_id"]: r for r in rows}


async def platform_with_counts(
    row: asyncpg.Record, counts: dict[Any, asyncpg.Record], redis: aioredis.Redis
) -> dict[str, Any]:
    c = counts.get(row["id"])
    last: datetime | None = (await stream_stats(redis, row["id"]))[1]
    return {
        **platform_wire(row),
        "firing": c["firing"] if c else 0,
        "acked": c["acked"] if c else 0,
        "resolved": c["resolved"] if c else 0,
        "last_event_at": last.isoformat() if last else None,
    }
