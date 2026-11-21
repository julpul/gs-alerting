"""`POST /ingest/{platform}`: Herald's webhook address on top of our ingest path.

The platform's token is our source key, sent as `X-Herald-Token` (or the key itself in the path, like
`/v1/ingest/{key}`). The body may be a JSON object, a JSON array (a batch) or text (one event per
line, see `app.pipeline.textlog`). Events go through the same key cache, envelope, preset split and
confirmed RabbitMQ publish as `/v1/ingest`, so the worker cannot tell the difference.
"""

from __future__ import annotations

import hmac
from typing import Any

import orjson
from fastapi import Header, Request, Response

from app.api.deps import Pool, RabbitDep
from app.api.routers.ingest import MAX_BATCH, MAX_BATCH_BYTES, _body, _publish, source_cache
from app.core.errors import AppError, ORJSONResponse, unauthorized
from app.core.metrics import INGESTED
from app.core.rabbit import Rabbit
from app.herald.platforms import PLATFORM_ALIASES
from app.herald.routing import herald_router
from app.pipeline.envelope import build_envelope, priority_for
from app.pipeline.presets import split_events
from app.pipeline.textlog import parse_text

router = herald_router()


def parse_body(body: bytes) -> list[dict[str, Any]]:
    """JSON object / array, or text (`textlog`: one event per line, `key=value` pairs, timestamp, level)."""
    try:
        parsed: Any = orjson.loads(body)
    except orjson.JSONDecodeError:
        parsed = parse_text(body.decode("utf-8", errors="replace"))
        if not parsed:
            raise AppError(422, "validation_error", "the body is empty") from None
    items = parsed if isinstance(parsed, list) else [parsed]
    if len(items) > MAX_BATCH:
        raise AppError(422, "validation_error", f"at most {MAX_BATCH} events per request")
    return [item if isinstance(item, dict) else {"value": item} for item in items]


async def publish_events(
    rabbit: Rabbit, source: dict[str, Any], raws: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Envelope (+ preset split) and publish with confirms, exactly like `/v1/ingest`."""
    priority = priority_for(source.get("default_severity"))
    envelopes = [
        build_envelope(source_id=source["id"], raw=part, priority=priority)
        for raw in raws
        for part in split_events(source.get("preset"), raw)
    ]
    if envelopes:
        await _publish(rabbit, envelopes, priority)
        INGESTED.labels(source=source["name"]).inc(len(envelopes))
    return envelopes


def _names(platform: str) -> set[str]:
    return {platform, PLATFORM_ALIASES.get(platform, platform)}


@router.post("/ingest/{platform}", status_code=202)
async def ingest(
    platform: str,
    request: Request,
    pool: Pool,
    rabbit: RabbitDep,
    x_herald_token: str | None = Header(default=None),
) -> Response:
    if x_herald_token:
        source = await source_cache.lookup(pool, x_herald_token)
        if source is None or not any(hmac.compare_digest(source["name"], n) for n in _names(platform)):
            raise unauthorized("bad or missing token")
    else:
        source = await source_cache.lookup(pool, platform)
        if source is None:
            raise unauthorized("bad or missing token")
    raws = parse_body(await _body(request, MAX_BATCH_BYTES))
    envelopes = await publish_events(rabbit, source, raws)
    first = envelopes[0] if envelopes else {"event_id": None, "received_at": None}
    return ORJSONResponse(
        {
            "accepted": True,
            "events": len(envelopes),
            "eventId": first["event_id"],
            "receivedAt": first["received_at"],
        },
        status_code=202,
    )
