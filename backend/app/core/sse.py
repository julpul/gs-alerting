"""SSE fan-out: publish only. API replicas subscribe to the Redis channels and stream to clients."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import orjson

from app.core.redis import get_redis, rkey


def team_channel(team_id: UUID | str) -> str:
    return rkey("sse", "team", str(team_id))


def user_channel(user_id: UUID | str) -> str:
    return rkey("sse", "user", str(user_id))


async def publish_sse(
    event_type: str,
    data: dict[str, Any],
    *,
    team_ids: Iterable[UUID] = (),
    user_ids: Iterable[UUID] = (),
) -> None:
    """Publish `{type, data, at}` to `sse:team:{id}` / `sse:user:{id}` channels (once per channel)."""
    message = orjson.dumps(
        {"type": event_type, "data": data, "at": datetime.now(UTC).isoformat(timespec="milliseconds")},
        default=str,
    ).decode()
    redis = get_redis()
    channels = {team_channel(t) for t in team_ids} | {user_channel(u) for u in user_ids}
    await asyncio.gather(*(redis.publish(ch, message) for ch in channels))
