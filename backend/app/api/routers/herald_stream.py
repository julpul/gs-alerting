"""`GET /api/stream`: our Redis SSE channels, re-framed in the Herald feed format.

Herald frames are `id: <n>` / `event: <kind>` / `data: {"id", "owner", "kind", "alert_id"}`; the UIs refetch
on any frame. Kinds: `alert` (alert.created|updated|acked|escalated|resolved, suggestion.ready),
`delivery` (delivery.status), `approval` (approval.requested|decided). `alert_id` is the alert short id.
Subscribed channels: `?owner=team:<slug>` (403 if not visible) or every team in the caller's scope, plus the
caller's own user channel. A `: keep-alive` comment is sent every `HEARTBEAT_SECONDS`.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import asyncpg
import orjson
from fastapi import Request
from fastapi.responses import StreamingResponse

from app.api.deps import Pool, RedisDep
from app.core.metrics import SSE_CLIENTS
from app.core.sse import team_channel, user_channel
from app.herald.identity import Actor
from app.herald.ids import owner_ref, short_from_ref, team_for_owner, team_slugs
from app.herald.routing import herald_router

log = logging.getLogger(__name__)
router = herald_router(prefix="/api", tags=["herald"])

HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 1.0
CACHE_SIZE = 2000


def kind_of(event_type: str) -> str:
    if event_type.startswith("alert.") or event_type == "suggestion.ready":
        return "alert"
    if event_type.startswith("delivery."):
        return "delivery"
    if event_type.startswith("approval."):
        return "approval"
    return event_type.split(".", 1)[0]


class Resolver:
    """Alert uuid -> (short id, team id) and team id -> slug, cached for the life of one stream."""

    def __init__(self, pool: asyncpg.Pool, slugs: dict[UUID, str]) -> None:
        self._pool = pool
        self._slugs = slugs
        self._alerts: OrderedDict[str, tuple[int, UUID]] = OrderedDict()

    async def alert(self, alert_id: str) -> tuple[int, UUID] | None:
        if alert_id in self._alerts:
            return self._alerts[alert_id]
        try:
            row = await self._pool.fetchrow(
                "SELECT short_id, team_id FROM alerts WHERE id = $1", UUID(alert_id)
            )
        except (ValueError, asyncpg.PostgresError):
            return None
        if row is None:
            return None
        self._alerts[alert_id] = (row["short_id"], row["team_id"])
        if len(self._alerts) > CACHE_SIZE:
            self._alerts.popitem(last=False)
        return self._alerts[alert_id]

    async def slug(self, team_id: UUID) -> str:
        if team_id not in self._slugs:
            self._slugs.update(await team_slugs_from_pool(self._pool))
        return self._slugs.get(team_id, str(team_id))


async def team_slugs_from_pool(pool: asyncpg.Pool) -> dict[UUID, str]:
    async with pool.acquire() as conn:
        return await team_slugs(conn)


async def translate(
    raw: str, event_id: int, resolver: Resolver, channel_team: UUID | None = None
) -> str | None:
    """Our Redis message `{type, data|payload, at}` -> one Herald frame (None when malformed).

    `channel_team` is the team whose channel carried the message (owner when the payload has none)."""
    try:
        msg = orjson.loads(raw)
        event_type = str(msg["type"])
    except (orjson.JSONDecodeError, KeyError, TypeError):
        log.warning("dropping malformed SSE message")
        return None
    data: dict[str, Any] = msg.get("payload", msg.get("data")) or {}
    kind = kind_of(event_type)
    short = short_from_ref(data["shortId"]) if kind == "alert" and data.get("shortId") else None
    team_id: UUID | None = None
    if data.get("teamId"):
        try:
            team_id = UUID(str(data["teamId"]))
        except ValueError:
            team_id = None
    alert_ref = data.get("alertId") or (data.get("id") if kind == "alert" else None)
    if alert_ref and (short is None or team_id is None):
        found = await resolver.alert(str(alert_ref))
        if found is not None:
            short = short if short is not None else found[0]
            team_id = team_id or found[1]
    team_id = team_id or channel_team
    owner = owner_ref(await resolver.slug(team_id)) if team_id else None
    body = {"id": event_id, "owner": owner, "kind": kind, "alert_id": short, "type": event_type}
    return f"id: {event_id}\nevent: {kind}\ndata: {orjson.dumps(body).decode()}\n\n"


async def _frames(
    request: Request, pubsub: Any, resolver: Resolver, first_id: int, teams: dict[str, UUID]
) -> AsyncIterator[str]:
    SSE_CLIENTS.inc()
    event_id = first_id
    last_sent = time.monotonic()
    try:
        yield ": connected\n\n"
        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=min(POLL_SECONDS, HEARTBEAT_SECONDS)
                )
            except Exception:
                log.exception("Herald SSE pub/sub read failed; closing so the client reconnects")
                return
            if message is not None and message.get("type") == "message":
                channel = message.get("channel")
                channel = channel.decode() if isinstance(channel, bytes) else str(channel)
                frame = await translate(message["data"], event_id + 1, resolver, teams.get(channel))
                if frame:
                    event_id += 1
                    last_sent = time.monotonic()
                    yield frame
            elif time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
                if await request.is_disconnected():
                    return
                last_sent = time.monotonic()
                yield ": keep-alive\n\n"
    finally:
        SSE_CLIENTS.dec()
        try:
            await pubsub.aclose()
        except Exception:
            log.debug("pubsub close failed", exc_info=True)


@router.get("/stream")
async def stream(request: Request, who: Actor, pool: Pool, redis: RedisDep, owner: str | None = None) -> Any:
    async with pool.acquire() as conn:
        slugs = await team_slugs(conn)
        if owner and owner.startswith("team:"):
            teams = [await team_for_owner(conn, owner, who.scope)]
        else:
            teams = sorted(who.scope.team_ids, key=str)
    by_channel = {team_channel(t): t for t in teams}
    pubsub = redis.pubsub()
    await pubsub.subscribe(*by_channel, user_channel(who.user_id))
    last = request.headers.get("last-event-id", "")
    return StreamingResponse(
        _frames(request, pubsub, Resolver(pool, slugs), int(last) if last.isdigit() else 0, by_channel),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
