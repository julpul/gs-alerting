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
from app.pipeline.presets import split_events
from app.pipeline.textlog import is_json_content_type, parse_text
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
            "SELECT id, name, team_id, domain_id, default_severity, preset FROM sources WHERE key_hash = $1",
            digest,
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


def body_events(body: bytes, content_type: str | None) -> tuple[list[dict[str, Any]] | None, Any]:
    """`(None, obj)` for a single JSON object; `(events, None)` for a JSON array (non-objects are wrapped
    as `{"value": x}`) or a text body (`textlog`: one event per line). A JSON content type must carry an
    object or an array; another content type falls back to text when the body is not JSON."""
    as_json = is_json_content_type(content_type)
    try:
        parsed: Any = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        if as_json or not body.strip():
            raise AppError(422, "validation_error", f"body is not valid JSON: {exc}") from exc
        parsed = None
    else:
        if isinstance(parsed, dict):
            return None, parsed
        if isinstance(parsed, list):
            return [e if isinstance(e, dict) else {"value": e} for e in parsed], None
        if as_json or not (content_type or "").lower().startswith("text/"):
            raise AppError(422, "validation_error", "body must be a JSON object, a JSON array or text lines")
    events = parse_text(body.decode("utf-8", errors="replace"))
    if not events:
        raise AppError(422, "validation_error", "the body has no non-empty line")
    return events, None


def envelopes_for(
    source: dict[str, Any], events: list[dict[str, Any]], priority: int
) -> list[dict[str, Any]]:
    if len(events) > MAX_BATCH:
        raise AppError(422, "validation_error", f"at most {MAX_BATCH} events per batch")
    preset = source.get("preset")
    return [
        build_envelope(source_id=source["id"], raw=part, priority=priority)
        for event in events
        for part in split_events(preset, event)
    ]


def _batch_accepted(envelopes: list[dict[str, Any]], *, single: bool = False) -> Response:
    """Batch shape; for a non-object body to the single endpoint also `eventId`/`receivedAt` of the first."""
    body: dict[str, Any] = {"accepted": len(envelopes), "eventIds": [e["event_id"] for e in envelopes]}
    if single:
        first = envelopes[0] if envelopes else {"event_id": None, "received_at": None}
        body.update(eventId=first["event_id"], receivedAt=first["received_at"])
    return ORJSONResponse(body, status_code=202)


@router.post("/v1/ingest/{sourceKey}", status_code=202)
async def ingest_event(
    sourceKey: str,
    request: Request,
    pool: Pool,
    rabbit: RabbitDep,
    x_api_key: str | None = Header(default=None),
) -> Response:
    """A JSON object (one event; a preset may split it), a JSON array (a batch) or text (one per line)."""
    source = await _source_for(pool, x_api_key if sourceKey == "_" else sourceKey)
    body = await _body(request, MAX_BATCH_BYTES)
    events, single = body_events(body, request.headers.get("content-type"))
    priority = priority_for(source["default_severity"])
    if events is None:
        if len(body) > MAX_EVENT_BYTES:
            raise AppError(413, "payload_too_large", f"request body exceeds {MAX_EVENT_BYTES} bytes")
        events = [single]
    envelopes = envelopes_for(source, events, priority)
    if envelopes:
        await _publish(rabbit, envelopes, priority)
        INGESTED.labels(source=source["name"]).inc(len(envelopes))
    if single is not None:
        return _accepted(envelopes[0])
    return _batch_accepted(envelopes, single=True)


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
    priority = priority_for(source["default_severity"])
    envelopes = envelopes_for(source, events, priority)
    if envelopes:
        await _publish(rabbit, envelopes, priority)
        INGESTED.labels(source=source["name"]).inc(len(envelopes))
    return _batch_accepted(envelopes)


async def _raise_source(pool: Pool, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Resolved as a dependency so a bad key is a 401 even when the body is invalid too."""
    return await _source_for(pool, x_api_key)


def typed_envelope(source: dict[str, Any], body: RaiseAlertRequest) -> dict[str, Any]:
    """Pre-normalized envelope (`typed: true`) for a typed alert raised by a system or an agent."""
    raw = body.model_dump(mode="json", exclude_none=True)
    attributes = body.attributes or {}
    severity = body.severity.value
    envelope = build_envelope(source_id=source["id"], raw=raw, priority=priority_for(severity), typed=True)
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
    return envelope


async def publish_typed(rabbit: Rabbit, source: dict[str, Any], body: RaiseAlertRequest) -> dict[str, Any]:
    """Build and publish a typed alert event (shared by `POST /v1/alerts/raise` and the MCP `raise_alert`)."""
    envelope = typed_envelope(source, body)
    await _publish(rabbit, [envelope], envelope["priority"])
    INGESTED.labels(source=source["name"]).inc()
    return envelope


@router.post("/v1/alerts/raise", status_code=202)
async def raise_alert(
    source: dict[str, Any] = Depends(_raise_source),
    body: RaiseAlertRequest = ...,
    rabbit: RabbitDep = ...,
) -> Response:
    """Typed alert API: same pipeline, the envelope is pre-normalized (`typed: true`)."""
    return _accepted(await publish_typed(rabbit, source, body))
