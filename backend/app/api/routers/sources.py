"""Sources API: list/create (key shown once), rotate key, samples, schema mapping (+ suggest), snippets."""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import CurrentScope, CurrentUser, Pool, RedisDep
from app.core.errors import AppError, ORJSONResponse, forbidden, not_found
from app.core.ids import parse_uuid
from app.pipeline.mapping_heuristic import suggest_mapping_heuristic
from app.pipeline.mapping_presets import preset_mapping
from app.repositories import sources as repo
from app.repositories.rules import can_edit_team, resolve_team

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sources", tags=["sources"])

MAPPING_FIELDS = {"severity", "service", "environment", "timestamp", "title", "type", "error_code"}


class RabbitConfig(BaseModel):
    url: str = Field(min_length=1, max_length=1000)
    queue: str = Field(min_length=1, max_length=255)


class CreateSource(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    teamId: str
    type: Literal["webhook", "rabbitmq"]
    preset: Literal["alertmanager", "sentry", "datadog", "cloudevents"] | None = None
    rabbitmq: RabbitConfig | None = None


class MappingBody(BaseModel):
    """`SchemaMapping` of the contract (+ the `type` and `error_code` fields of spec 6)."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["draft", "accepted"] = "accepted"
    detectedSchema: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    severityValueMap: dict[str, Literal["critical", "error", "warning", "info"]] = Field(default_factory=dict)
    explanation: str | None = None

    @field_validator("fields")
    @classmethod
    def _known_fields(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - MAPPING_FIELDS)
        if unknown:
            raise ValueError(f"unknown mapping field(s): {', '.join(unknown)}")
        if any(not v or len(v) > 200 for v in value.values()):
            raise ValueError("paths must be 1-200 characters")
        return value


class SuggestRequest(BaseModel):
    sample: dict[str, Any]


def _source_id(raw: str) -> UUID:
    source_id = parse_uuid(raw)
    if source_id is None:
        raise not_found("source")
    return source_id


async def _visible(conn, scope, source_id: str):
    row = await repo.get_source(conn, scope, _source_id(source_id))
    if row is None:
        raise not_found("source")
    return row


async def _editable(conn, scope, source_id: str):
    row = await _visible(conn, scope, source_id)
    if not await can_edit_team(conn, scope, row["team_id"]):
        raise forbidden("you cannot edit sources of this team")
    return row


@router.get("")
async def list_sources(pool: Pool, redis: RedisDep, scope: CurrentScope) -> Response:
    async with pool.acquire() as conn:
        rows = await repo.list_sources(conn, scope)
    out = []
    for row in rows:
        count, last = await repo.stream_stats(redis, row["id"])
        out.append(repo.source_wire(row, events_24h=count, last_event_at=last))
    return ORJSONResponse(out)


@router.post("", status_code=201)
async def create_source(body: CreateSource, pool: Pool, user: CurrentUser, scope: CurrentScope) -> Response:
    if body.type == "rabbitmq" and body.rabbitmq is None:
        raise AppError(422, "validation_error", "rabbitmq: url and queue are required", {"path": "rabbitmq"})
    async with pool.acquire() as conn:
        team_id = await resolve_team(conn, body.teamId)
        if team_id is None:
            raise AppError(422, "validation_error", "teamId: unknown team", {"path": "teamId"})
        if not await can_edit_team(conn, scope, team_id):
            raise forbidden("you cannot create sources for this team")
        domain_id = await repo.team_domain(conn, team_id)
        if domain_id is None:
            raise AppError(422, "validation_error", "teamId: team has no domain", {"path": "teamId"})
        config = body.rabbitmq.model_dump() if body.type == "rabbitmq" and body.rabbitmq else {}
        async with conn.transaction():
            row, key = await repo.create_source(
                conn,
                name=body.name,
                team_id=team_id,
                domain_id=domain_id,
                type_=body.type,
                preset=body.preset if body.type == "webhook" else None,
                config=config,
                mapping=preset_mapping(body.preset) if body.type == "webhook" else None,
                user=user,
            )
    return ORJSONResponse(repo.source_wire(row, api_key=key), status_code=201)


@router.post("/{source_id}/rotate-key")
async def rotate_key(
    source_id: str, pool: Pool, redis: RedisDep, user: CurrentUser, scope: CurrentScope
) -> Response:
    async with pool.acquire() as conn:
        row = await _editable(conn, scope, source_id)
        async with conn.transaction():
            key = await repo.rotate_key(conn, row, user)
        row = await repo.get_source(conn, scope, row["id"])
    count, last = await repo.stream_stats(redis, row["id"])
    return ORJSONResponse(repo.source_wire(row, events_24h=count, last_event_at=last, api_key=key))


@router.get("/{source_id}/sample-events")
async def sample_events(
    source_id: str, pool: Pool, redis: RedisDep, scope: CurrentScope, limit: int = Query(50, ge=1, le=200)
) -> Response:
    async with pool.acquire() as conn:
        row = await _visible(conn, scope, source_id)
    events = await repo.read_recent(redis, row["id"], limit=limit)
    return ORJSONResponse(
        [{"eventId": e.event_id, "receivedAt": e.received_at, "payload": e.raw} for e in events]
    )


async def _suggest(sample: dict[str, Any], row) -> dict[str, Any]:
    try:
        from app.ai.mapping import suggest_mapping
    except ImportError:
        return suggest_mapping_heuristic(sample)
    source = {k: row[k] for k in ("id", "name", "type", "preset", "default_severity")}
    try:
        draft = await suggest_mapping(sample, source)
    except Exception as exc:
        log.warning("AI mapping suggestion failed, using heuristic: %s", exc)
        return suggest_mapping_heuristic(sample)
    out = (
        draft if isinstance(draft, dict) else draft.model_dump(by_alias=True, exclude_none=True, mode="json")
    )
    return {**out, "status": "draft"}


@router.post("/{source_id}/mapping/suggest")
async def suggest_mapping_route(
    source_id: str, body: SuggestRequest, pool: Pool, scope: CurrentScope
) -> Response:
    async with pool.acquire() as conn:
        row = await _editable(conn, scope, source_id)
    return ORJSONResponse(await _suggest(body.sample, row))


@router.put("/{source_id}/mapping")
async def save_mapping(
    source_id: str, body: MappingBody, pool: Pool, user: CurrentUser, scope: CurrentScope
) -> Response:
    mapping = body.model_dump(exclude_none=True)
    async with pool.acquire() as conn:
        row = await _editable(conn, scope, source_id)
        async with conn.transaction():
            await repo.save_mapping(conn, row, mapping, user)
    return ORJSONResponse(mapping)


@router.get("/{source_id}/snippets")
async def snippets(source_id: str, pool: Pool, scope: CurrentScope) -> Response:
    async with pool.acquire() as conn:
        await _visible(conn, scope, source_id)
    url = repo.ingest_url()
    sample = '{"severity": "ERROR", "message": "something broke"}'
    return ORJSONResponse(
        {
            "curl": f"curl -X POST '{url}' \\\n  -H 'Content-Type: application/json' \\\n  -d '{sample}'",
            "python": (
                "import httpx\n\n"
                f'httpx.post("{url}",\n'
                '           json={"severity": "ERROR", "message": "something broke"}).raise_for_status()\n'
            ),
            "js": (
                f'await fetch("{url}", {{\n'
                '  method: "POST",\n'
                '  headers: { "Content-Type": "application/json" },\n'
                '  body: JSON.stringify({ severity: "ERROR", message: "something broke" }),\n'
                "});\n"
            ),
            "java": (
                "var request = java.net.http.HttpRequest.newBuilder()\n"
                f'    .uri(java.net.URI.create("{url}"))\n'
                '    .header("Content-Type", "application/json")\n'
                f"    .POST(java.net.http.HttpRequest.BodyPublishers.ofString({_java_string(sample)}))\n"
                "    .build();\n"
                "java.net.http.HttpClient.newHttpClient()\n"
                "    .send(request, java.net.http.HttpResponse.BodyHandlers.ofString());\n"
            ),
        }
    )


def _java_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
