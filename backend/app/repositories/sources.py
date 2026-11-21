"""Sources persistence + the Redis `recent:{source_id}` sample streams. Reads are filtered by `team_id`."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
import orjson
import redis.asyncio as aioredis

from app.config import get_settings
from app.core.audit import audit, user_actor
from app.core.ids import uuid7
from app.core.redis import rkey
from app.core.scope import Scope
from app.core.security import generate_secret_token
from app.schemas.internal import User

SOURCE_COLUMNS = (
    "id, name, team_id, domain_id, type, preset, key_prefix, config, mapping, mapping_status, "
    "default_severity, created_at"
)
KEY_PREFIX = "sk_"
STREAM_CAP = 5000


@dataclass
class RecentEvent:
    event_id: str
    received_at: datetime
    raw: dict[str, Any]
    meta: dict[str, Any]


# ---------- Redis recent streams ----------


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def parse_stream_entry(entry_id: str, fields: dict[str, str]) -> RecentEvent | None:
    """A `recent:{id}` entry: field `e` = JSON of the envelope (`raw`, `_meta`, `event_id`, `received_at`)."""
    try:
        env = orjson.loads(fields["e"])
    except (KeyError, orjson.JSONDecodeError, TypeError):
        return None
    if not isinstance(env, dict):
        return None
    raw, meta = (env["raw"], env.get("_meta") or {}) if "raw" in env else (env, {})
    if not isinstance(raw, dict):
        return None
    received = _ms_to_dt(int(entry_id.split("-")[0]))
    if isinstance(env.get("received_at"), str):
        try:
            received = datetime.fromisoformat(env["received_at"].replace("Z", "+00:00"))
        except ValueError:
            pass
    return RecentEvent(
        str(env.get("event_id") or entry_id), received, raw, meta if isinstance(meta, dict) else {}
    )


async def read_recent(
    redis: aioredis.Redis, source_id: UUID, *, limit: int, since_ms: int | None = None
) -> list[RecentEvent]:
    """Newest first."""
    low = f"{since_ms}-0" if since_ms is not None else "-"
    entries = await redis.xrevrange(rkey("recent", str(source_id)), max="+", min=low, count=limit)
    return [ev for entry_id, fields in entries if (ev := parse_stream_entry(entry_id, fields)) is not None]


async def stream_stats(redis: aioredis.Redis, source_id: UUID) -> tuple[int, datetime | None]:
    """(events in the last 24 h, last event time). Bounded by the stream cap (~5000)."""
    key = rkey("recent", str(source_id))
    newest = await redis.xrevrange(key, count=1)
    if not newest:
        return 0, None
    last = _ms_to_dt(int(newest[0][0].split("-")[0]))
    since = int((time.time() - 86400) * 1000)
    oldest = await redis.xrange(key, count=1)
    if oldest and int(oldest[0][0].split("-")[0]) >= since:
        return await redis.xlen(key), last
    entries = await redis.xrange(key, min=f"{since}-0", count=STREAM_CAP)
    return len(entries), last


# ---------- DB ----------


async def list_sources(conn: asyncpg.Connection, scope: Scope) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"SELECT {SOURCE_COLUMNS} FROM sources WHERE team_id = ANY($1) ORDER BY name, id",
        list(scope.team_ids),
    )


async def get_source(conn: asyncpg.Connection, scope: Scope, source_id: UUID) -> asyncpg.Record | None:
    row = await conn.fetchrow(f"SELECT {SOURCE_COLUMNS} FROM sources WHERE id = $1", source_id)
    if row is None or not scope.can_see_team(row["team_id"]):
        return None
    return row


async def team_domain(conn: asyncpg.Connection, team_id: UUID) -> UUID | None:
    return await conn.fetchval(
        "SELECT td.domain_id FROM team_domains td JOIN domains d ON d.id = td.domain_id "
        "WHERE td.team_id = $1 ORDER BY d.slug LIMIT 1",
        team_id,
    )


async def create_source(
    conn: asyncpg.Connection,
    *,
    name: str,
    team_id: UUID,
    domain_id: UUID,
    type_: str,
    preset: str | None,
    config: dict[str, Any],
    mapping: dict[str, Any] | None,
    user: User,
) -> tuple[asyncpg.Record, str]:
    key, prefix, key_hash = generate_secret_token(KEY_PREFIX)
    source_id = uuid7()
    await conn.execute(
        "INSERT INTO sources (id, name, team_id, domain_id, type, preset, key_prefix, key_hash, config, "
        "mapping, mapping_status) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        source_id,
        name,
        team_id,
        domain_id,
        type_,
        preset,
        prefix,
        key_hash,
        config,
        mapping,
        mapping["status"] if mapping else None,
    )
    await audit(
        conn,
        user_actor(user.id, user.display_name),
        "source.create",
        "source",
        str(source_id),
        {"name": name, "teamId": str(team_id), "type": type_},
    )
    row = await conn.fetchrow(f"SELECT {SOURCE_COLUMNS} FROM sources WHERE id = $1", source_id)
    return row, key


async def rotate_key(conn: asyncpg.Connection, source: asyncpg.Record, user: User) -> str:
    key, prefix, key_hash = generate_secret_token(KEY_PREFIX)
    await conn.execute(
        "UPDATE sources SET key_prefix = $2, key_hash = $3 WHERE id = $1", source["id"], prefix, key_hash
    )
    await audit(
        conn, user_actor(user.id, user.display_name), "source.rotate_key", "source", str(source["id"])
    )
    return key


async def save_mapping(
    conn: asyncpg.Connection, source: asyncpg.Record, mapping: dict[str, Any], user: User
) -> None:
    await conn.execute(
        "UPDATE sources SET mapping = $2, mapping_status = $3 WHERE id = $1",
        source["id"],
        mapping,
        mapping["status"],
    )
    await audit(
        conn,
        user_actor(user.id, user.display_name),
        "source.mapping",
        "source",
        str(source["id"]),
        {"status": mapping["status"]},
    )


# ---------- wire ----------


def ingest_url(key: str | None = None) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/v1/ingest/{key or 'YOUR_SOURCE_KEY'}"


def source_wire(
    row: asyncpg.Record,
    *,
    events_24h: int = 0,
    last_event_at: datetime | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    out = {
        "id": str(row["id"]),
        "name": row["name"],
        "teamId": str(row["team_id"]),
        "type": row["type"],
        "preset": row["preset"],
        "ingestUrl": ingest_url(api_key),
        "eventsLast24h": events_24h,
        "lastEventAt": last_event_at,
        "mappingStatus": row["mapping_status"] or "none",
    }
    if api_key is not None:
        out["apiKey"] = api_key
    return out
