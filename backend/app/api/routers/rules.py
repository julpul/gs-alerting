"""Rules API: CRUD with versions, YAML round-trip, dry-run, AI rule copilot (`from-text`)."""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

import orjson
from fastapi import APIRouter, Body, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import CurrentScope, CurrentUser, Pool, RedisDep
from app.core.errors import AppError, ORJSONResponse, forbidden, not_found
from app.core.ids import parse_uuid
from app.core.redis import rkey
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.dryrun import dry_run
from app.pipeline.rules.index import CHANNEL, rule_from_row
from app.pipeline.rules.model import RuleDef, RuleValidationError, parse_rule
from app.pipeline.rules.yamlio import dump_rule_yaml, load_rule_yaml
from app.repositories import rules as repo

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/rules", tags=["rules"])

RuleBody = Annotated[dict[str, Any], Body()]


class DryRunRequest(BaseModel):
    rule: dict[str, Any]
    lookbackMinutes: int = Field(60, ge=1, le=10080)


class FromTextRequest(BaseModel):
    text: str
    teamId: str


def _invalid(exc: RuleValidationError) -> AppError:
    return AppError(422, "invalid_rule", f"{exc.path}: {exc.message}".lstrip(": "), {"path": exc.path})


def _rule_id(raw: str) -> UUID:
    rule_id = parse_uuid(raw)
    if rule_id is None:
        raise not_found("rule")
    return rule_id


async def _prepare(conn, data: Any) -> RuleDef:
    """Resolve the team (slug or uuid), validate the model and compile it. Raises 422 `invalid_rule`."""
    try:
        if not isinstance(data, dict):
            raise RuleValidationError("", "rule must be an object")
        data = {k: v for k, v in data.items() if k not in ("id", "version")}
        ref = data.pop("team_id", None) or data.get("teamId")
        if ref is not None:
            team_id = await repo.resolve_team(conn, str(ref))
            if team_id is None:
                raise RuleValidationError("teamId", "unknown team")
            data["teamId"] = str(team_id)
        rule = parse_rule(data)
        compile_rule(rule)
    except RuleValidationError as exc:
        raise _invalid(exc) from exc
    return rule


async def _announce(redis, rule_id: UUID, version: int | None) -> None:
    await redis.publish(rkey(CHANNEL), orjson.dumps({"ruleId": str(rule_id), "version": version}))


async def _editable_record(conn, scope, rule_id: UUID):
    row = await repo.get_record(conn, scope, rule_id)
    if row is None:
        raise not_found("rule")
    if not await repo.can_edit_team(conn, scope, row["team_id"]):
        raise forbidden("you cannot edit rules of this team")
    return row


async def _save_update(conn, redis, scope, user, row, data: Any) -> dict[str, Any]:
    rule = await _prepare(conn, data)
    if rule.team_id != row["team_id"] and not await repo.can_edit_team(conn, scope, rule.team_id):
        raise forbidden("you cannot move rules to that team")
    async with conn.transaction():
        saved = await repo.update_rule(conn, row, rule, user)
    await _announce(redis, saved.id, saved.version)
    return saved.to_wire()


@router.get("")
async def list_rules(pool: Pool, scope: CurrentScope, teamId: str | None = None) -> Response:
    async with pool.acquire() as conn:
        team_id = None
        if teamId is not None:
            team_id = await repo.resolve_team(conn, teamId)
            if team_id is None:
                return ORJSONResponse([])
        return ORJSONResponse(await repo.list_rules(conn, scope, team_id))


@router.post("", status_code=201)
async def create_rule(
    body: RuleBody, pool: Pool, redis: RedisDep, user: CurrentUser, scope: CurrentScope
) -> Response:
    async with pool.acquire() as conn:
        rule = await _prepare(conn, body)
        if not await repo.can_edit_team(conn, scope, rule.team_id):
            raise forbidden("you cannot create rules for this team")
        async with conn.transaction():
            saved = await repo.insert_rule(conn, rule, user)
    await _announce(redis, saved.id, saved.version)
    return ORJSONResponse(saved.to_wire(), status_code=201)


@router.post("/dry-run")
async def dry_run_rule(body: DryRunRequest, pool: Pool, redis: RedisDep, scope: CurrentScope) -> Response:
    async with pool.acquire() as conn:
        rule = await _prepare(conn, body.rule)
        if not scope.can_see_team(rule.team_id):
            raise forbidden("team not in your scope")
        return ORJSONResponse(await dry_run(conn, redis, rule, lookback_minutes=body.lookbackMinutes))


@router.post("/from-text")
async def rule_from_text(body: FromTextRequest, pool: Pool, redis: RedisDep, scope: CurrentScope) -> Response:
    try:
        from app.ai.rule_copilot import draft_rule
    except ImportError as exc:
        raise AppError(503, "ai_unavailable", "AI rule copilot is not available") from exc
    async with pool.acquire() as conn:
        team_id = await repo.resolve_team(conn, body.teamId)
        if team_id is None or not scope.can_see_team(team_id):
            raise forbidden("team not in your scope")
        try:
            draft, explanation = await draft_rule(body.text, scope, conn)
        except AppError:
            raise
        except Exception as exc:
            log.warning("rule copilot failed: %s", exc)
            raise AppError(503, "ai_unavailable", "AI rule copilot is not available") from exc
        try:
            result = await dry_run(conn, redis, draft)
        except RuleValidationError as exc:
            raise _invalid(exc) from exc
    return ORJSONResponse({"draft": draft.to_wire(), "explanation": explanation, "dryRun": result})


@router.get("/{rule_id}")
async def get_rule(rule_id: str, pool: Pool, scope: CurrentScope) -> Response:
    async with pool.acquire() as conn:
        row = await repo.get_record(conn, scope, _rule_id(rule_id))
        if row is None:
            raise not_found("rule")
        return ORJSONResponse(rule_from_row(row).to_wire())


@router.put("/{rule_id}")
async def update_rule(
    rule_id: str, body: RuleBody, pool: Pool, redis: RedisDep, user: CurrentUser, scope: CurrentScope
) -> Response:
    async with pool.acquire() as conn:
        row = await _editable_record(conn, scope, _rule_id(rule_id))
        return ORJSONResponse(await _save_update(conn, redis, scope, user, row, body))


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str, pool: Pool, redis: RedisDep, user: CurrentUser, scope: CurrentScope
) -> Response:
    async with pool.acquire() as conn:
        row = await _editable_record(conn, scope, _rule_id(rule_id))
        async with conn.transaction():
            await repo.delete_rule(conn, row, user)
    await _announce(redis, row["id"], None)
    return Response(status_code=204)


@router.get("/{rule_id}/versions")
async def rule_versions(rule_id: str, pool: Pool, scope: CurrentScope) -> Response:
    async with pool.acquire() as conn:
        row = await repo.get_record(conn, scope, _rule_id(rule_id))
        if row is None:
            raise not_found("rule")
        return ORJSONResponse(await repo.list_versions(conn, row["id"]))


@router.get("/{rule_id}/yaml")
async def get_rule_yaml(rule_id: str, pool: Pool, scope: CurrentScope) -> Response:
    async with pool.acquire() as conn:
        row = await repo.get_record(conn, scope, _rule_id(rule_id))
        if row is None:
            raise not_found("rule")
        wire = rule_from_row(row).to_wire()
        slug = await repo.team_slug(conn, row["team_id"])
    out = {"name": wire.pop("name"), "teamId": slug or wire.pop("teamId")}
    wire.pop("teamId", None)
    wire.pop("id", None)
    wire.pop("version", None)
    return Response(dump_rule_yaml({**out, **wire}), media_type="application/yaml")


@router.put("/{rule_id}/yaml")
async def put_rule_yaml(
    rule_id: str, request: Request, pool: Pool, redis: RedisDep, user: CurrentUser, scope: CurrentScope
) -> Response:
    text = (await request.body()).decode("utf-8", errors="replace")
    try:
        data = load_rule_yaml(text)
    except RuleValidationError as exc:
        raise _invalid(exc) from exc
    async with pool.acquire() as conn:
        row = await _editable_record(conn, scope, _rule_id(rule_id))
        return ORJSONResponse(await _save_update(conn, redis, scope, user, row, data))
