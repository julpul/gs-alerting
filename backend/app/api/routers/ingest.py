"""Ingest endpoints (spec 4.4.1, 5.1): durable write to RabbitMQ, then `202`."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

import asyncpg
import orjson
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response

from app.api.deps import Pool, RabbitDep
from app.core.errors import AppError, ORJSONResponse, unauthorized
from app.core.metrics import INGESTED
from app.core.rabbit import EVENTS_EXCHANGE, EVENTS_QUEUE, Rabbit
from app.core.security import hash_token
from app.pipeline.envelope import MAX_EVENT_BYTES, build_envelope, priority_for
from app.schemas.api import RaiseAlertRequest

log = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])

MAX_BATCH = 1000
MAX_BATCH_BYTES = 16 * 1024 * 1024
PUBLISH_TIMEOUT = 5.0
RETRY_AFTER_SECONDS = "2"
SOURCE_KEY_TTL = 60.0
UNKNOWN_KEY_TTL = 5.0
CACHE_SIZE = 2048


class SourceKeyCache:
    """In-process LRU/TTL cache over `sources.key_hash` (60 s; unknown keys are cached 5 s)."""

    def __init__(
        self, ttl: float = SOURCE_KEY_TTL, negative_ttl: float = UNKNOWN_KEY_TTL, size: int = CACHE_SIZE
    ) -> None:
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self.size = size
        self._items: OrderedDict[str, tuple[float, dict[str, Any] | None]] = OrderedDict()

    def clear(self) -> None:
        self._items.clear()

    async def lookup(self, pool: asyncpg.Pool, key: str) -> dict[str, Any] | None:
        digest = hash_token(key)
        now = time.monotonic()
        hit = self._items.get(digest)
        if hit and hit[0] > now:
            self._items.move_to_end(digest)
            return hit[1]
        row = await pool.fetchrow(
            "SELECT id, name, team_id, domain_id, default_severity FROM sources WHERE key_hash = $1", digest
        )
        source = dict(row) if row else None
        self._items[digest] = (now + (self.ttl if source else self.negative_ttl), source)
        self._items.move_to_end(digest)
        while len(self._items) > self.size:
            self._items.popitem(last=False)
        return source


source_cache = SourceKeyCache()


async def _source_for(pool: asyncpg.Pool, key: str | None) -> dict[str, Any]:
    source = await source_cache.lookup(pool, key) if key else None
    if source is None:
        raise unauthorized("invalid or missing source key")
    return source


async def _body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise AppError(413, "payload_too_large", f"request body exceeds {limit} bytes")
    body = await request.body()
    if len(body) > limit:
        raise AppError(413, "payload_too_large", f"request body exceeds {limit} bytes")
    return body


def _json(body: bytes) -> Any:
    try:
        return orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        raise AppError(422, "validation_error", f"body is not valid JSON: {exc}") from exc


def _unavailable() -> HTTPException:
    return HTTPException(
        503, "event queue unavailable; retry shortly", headers={"Retry-After": RETRY_AFTER_SECONDS}
    )


async def _publish(rabbit: Rabbit, envelopes: list[dict[str, Any]], priority: int) -> None:
    """Publish with confirms; any broker failure is a 503 (the producer retries, nothing is lost)."""
    try:
        if len(envelopes) == 1:
            publish = rabbit.publish(EVENTS_EXCHANGE, EVENTS_QUEUE, envelopes[0], priority=priority)
        else:
            publish = rabbit.publish_batch(EVENTS_EXCHANGE, EVENTS_QUEUE, envelopes, priority=priority)
        await asyncio.wait_for(publish, PUBLISH_TIMEOUT)
    except Exception as exc:
        log.warning("publish to %s failed: %r", EVENTS_QUEUE, exc)
        raise _unavailable() from exc


def _accepted(envelope: dict[str, Any]) -> Response:
    return ORJSONResponse(
        {"eventId": envelope["event_id"], "receivedAt": envelope["received_at"]}, status_code=202
    )


@router.post("/v1/ingest/{sourceKey}", status_code=202)
async def ingest_event(
    sourceKey: str,
    request: Request,
    pool: Pool,
    rabbit: RabbitDep,
    x_api_key: str | None = Header(default=None),
) -> Response:
    source = await _source_for(pool, x_api_key if sourceKey == "_" else sourceKey)
    raw = _json(await _body(request, MAX_EVENT_BYTES))
    if not isinstance(raw, dict):
        raise AppError(422, "validation_error", "body must be a JSON object")
    priority = priority_for(source["default_severity"])
    envelope = build_envelope(source_id=source["id"], raw=raw, priority=priority)
    await _publish(rabbit, [envelope], priority)
    INGESTED.labels(source=source["name"]).inc()
    return _accepted(envelope)


@router.post("/v1/ingest/{sourceKey}/batch", status_code=202)
async def ingest_batch(
    sourceKey: str,
    request: Request,
    pool: Pool,
    rabbit: RabbitDep,
    x_api_key: str | None = Header(default=None),
) -> Response:
    source = await _source_for(pool, x_api_key if sourceKey == "_" else sourceKey)
    events = _json(await _body(request, MAX_BATCH_BYTES))
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        raise AppError(422, "validation_error", "body must be a JSON array of objects")
    if len(events) > MAX_BATCH:
        raise AppError(422, "validation_error", f"at most {MAX_BATCH} events per batch")
    priority = priority_for(source["default_severity"])
    envelopes = [build_envelope(source_id=source["id"], raw=e, priority=priority) for e in events]
    if envelopes:
        await _publish(rabbit, envelopes, priority)
        INGESTED.labels(source=source["name"]).inc(len(envelopes))
    return ORJSONResponse(
        {"accepted": len(envelopes), "eventIds": [e["event_id"] for e in envelopes]}, status_code=202
    )


async def _raise_source(pool: Pool, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Resolved as a dependency so a bad key is a 401 even when the body is invalid too."""
    return await _source_for(pool, x_api_key)


@router.post("/v1/alerts/raise", status_code=202)
async def raise_alert(
    source: dict[str, Any] = Depends(_raise_source),
    body: RaiseAlertRequest = ...,
    rabbit: RabbitDep = ...,
) -> Response:
    """Typed alert API: same pipeline, the envelope is pre-normalized (`typed: true`)."""
    raw = body.model_dump(mode="json", exclude_none=True)
    attributes = body.attributes or {}
    severity = body.severity.value
    priority = priority_for(severity)
    envelope = build_envelope(source_id=source["id"], raw=raw, priority=priority, typed=True)
    envelope["_meta"] = {
        "source": source["name"],
        "source_name": source["name"],
        "schema": body.type,
        "severity": severity,
        "service": attributes.get("service"),
        "environment": attributes.get("environment") or attributes.get("env"),
        "type": body.type,
        "title": body.title,
        "timestamp": envelope["received_at"],
        "error_code": attributes.get("error_code") or attributes.get("errorCode"),
    }
    await _publish(rabbit, [envelope], priority)
    INGESTED.labels(source=source["name"]).inc()
    return _accepted(envelope)
