"""GET /v1/stream: Server-Sent Events fed by the Redis channels `sse:team:{id}` / `sse:user:{id}`.

One Redis pub/sub connection per client. Each frame is `event: <type>`, `id: <n>`, `data: <StreamEvent
JSON>` followed by a blank line; a `: hb` comment is sent every `HEARTBEAT_SECONDS` (tests shorten it).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentScope, CurrentUser, RedisDep
from app.core.errors import AppError, forbidden
from app.core.ids import parse_uuid
from app.core.metrics import SSE_CLIENTS
from app.core.sse import team_channel, user_channel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["stream"])

HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 1.0


def format_event(raw: str, event_id: int) -> str | None:
    """Redis message `{type, data|payload, at}` -> SSE frame carrying a contract `StreamEvent`."""
    try:
        msg = orjson.loads(raw)
        event_type = msg["type"]
    except (orjson.JSONDecodeError, KeyError, TypeError):
        log.warning("dropping malformed SSE message")
        return None
    event = {
        "type": event_type,
        "at": msg.get("at") or datetime.now(UTC).isoformat(timespec="milliseconds"),
        "payload": msg.get("payload", msg.get("data")) or {},
    }
    return f"event: {event_type}\nid: {event_id}\ndata: {orjson.dumps(event, default=str).decode()}\n\n"


async def _frames(request: Request, pubsub: Any, first_id: int) -> AsyncIterator[str]:
    SSE_CLIENTS.inc()
    event_id = first_id
    last_sent = time.monotonic()
    try:
        yield "retry: 3000\n\n"
        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=min(POLL_SECONDS, HEARTBEAT_SECONDS)
                )
            except Exception:
                log.exception("SSE pub/sub read failed; closing stream so the client reconnects")
                return
            if message is not None and message.get("type") == "message":
                event_id += 1
                frame = format_event(message["data"], event_id)
                if frame:
                    last_sent = time.monotonic()
                    yield frame
            elif time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
                if await request.is_disconnected():
                    return
                last_sent = time.monotonic()
                yield ": hb\n\n"
    finally:
        SSE_CLIENTS.dec()
        try:
            await pubsub.aclose()
        except Exception:
            log.debug("pubsub close failed", exc_info=True)


@router.get("/stream")
async def stream(
    request: Request, user: CurrentUser, scope: CurrentScope, redis: RedisDep, teamId: str | None = None
) -> StreamingResponse:
    if teamId:
        team = parse_uuid(teamId)
        if team is None:
            raise AppError(422, "validation_error", "teamId: must be a uuid")
        if not scope.can_see_team(team):
            raise forbidden("team is not in your scope")
        teams = [team]
    else:
        teams = sorted(scope.team_ids, key=str)
    channels = [team_channel(t) for t in teams] + [user_channel(user.id)]

    pubsub = redis.pubsub()
    await pubsub.subscribe(*channels)
    last_event_id = request.headers.get("last-event-id", "")
    first_id = int(last_event_id) if last_event_id.isdigit() else 0
    return StreamingResponse(
        _frames(request, pubsub, first_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
