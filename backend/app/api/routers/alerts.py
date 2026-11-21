"""Alerts REST: list, detail, ack, resolve, assign, comments (spec 15)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Query
from pydantic import AwareDatetime, BaseModel, Field

from app.api.deps import CurrentScope, CurrentUser, Pool, RabbitDep
from app.core.errors import AppError, not_found
from app.core.ids import parse_uuid
from app.core.outbox import publish_outbox_rows
from app.core.sse import publish_sse
from app.repositories import alert_actions as actions
from app.repositories import alerts as repo
from app.schemas import api as wire

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])


class AlertSummaryOut(wire.AlertSummary):
    shortId: str
    escalatesAt: AwareDatetime | None = None


class DeliveryOut(wire.Delivery):
    status: str | None = None


class TimelineOut(wire.TimelineEntry):
    type: str | None = None


class AlertDetailOut(wire.AlertDetail):
    shortId: str
    escalatesAt: AwareDatetime | None = None
    suggestion: wire.Suggestion | None = None
    ackedAt: AwareDatetime | None = None
    resolvedAt: AwareDatetime | None = None
    nextRenotifyAt: AwareDatetime | None = None
    deliveries: list[DeliveryOut] | None = None
    timeline: list[TimelineOut] | None = None


class AlertPage(BaseModel):
    items: list[AlertSummaryOut]
    nextCursor: str | None = None


class ResolveBody(BaseModel):
    resolutionNote: str | None = Field(default=None, max_length=10_000)


class AssignBody(BaseModel):
    userId: str


class CommentBody(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


def _enum_list(values: list[str] | None, allowed: set[str], name: str) -> list[str] | None:
    """Accept `a,b` (contract: form, explode=false) and repeated `a&a`."""
    if not values:
        return None
    out = [p.strip() for v in values for p in v.split(",") if p.strip()]
    bad = [v for v in out if v not in allowed]
    if bad:
        raise AppError(422, "validation_error", f"{name}: invalid value {bad[0]!r}")
    return out or None


def _uuid_param(value: str | None, name: str) -> UUID | None:
    if value is None or value == "":
        return None
    parsed = parse_uuid(value)
    if parsed is None:
        raise AppError(422, "validation_error", f"{name}: must be a uuid")
    return parsed


async def _safe_publish(event: str, data: dict[str, Any], team_id: UUID) -> None:
    try:
        await publish_sse(event, data, team_ids=[team_id])
    except Exception:
        log.exception("SSE publish of %s failed", event)


async def _publish_outbox(pool: asyncpg.Pool, rabbit: Any, ids: list[int]) -> None:
    try:
        await publish_outbox_rows(pool, rabbit, ids)
    except Exception:
        log.exception("outbox publish failed; relay will retry")


async def _mutate(
    pool: asyncpg.Pool,
    ref: str,
    scope: Any,
    work: Callable[[asyncpg.Connection, asyncpg.Record], Any],
    *,
    needs_role: bool = True,
) -> tuple[Any, dict[str, Any], asyncpg.Record]:
    """Lock the visible alert, check the role, run `work` and return (result, summary, alert row)."""
    async with pool.acquire() as conn, conn.transaction():
        row = await repo.find_alert(conn, ref, scope, for_update=True)
        if row is None:
            raise not_found("alert")
        if needs_role:
            await actions.require_actor_role(conn, scope, row)
        result = await work(conn, row)
        fresh = await repo.find_alert(conn, ref, scope)
    return result, repo.summary_out(fresh), row


@router.get("", response_model=AlertPage)
async def list_alerts(
    scope: CurrentScope,
    pool: Pool,
    status: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[list[str] | None, Query()] = None,
    teamId: str | None = None,
    domainId: str | None = None,
    sourceId: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=repo.MAX_LIMIT)] = 50,
) -> dict[str, Any]:
    statuses = _enum_list(status, {s.value for s in wire.AlertStatus}, "status")
    severities = _enum_list(severity, {s.value for s in wire.Severity}, "severity")
    async with pool.acquire() as conn:
        items, next_cursor = await repo.list_alerts(
            conn,
            scope,
            statuses=statuses,
            severities=severities,
            team_id=_uuid_param(teamId, "teamId"),
            domain_id=_uuid_param(domainId, "domainId"),
            source_id=_uuid_param(sourceId, "sourceId"),
            q=q,
            cursor=cursor,
            limit=limit,
        )
    return {"items": items, "nextCursor": next_cursor}


@router.get("/{alert_id}", response_model=AlertDetailOut)
async def get_alert(alert_id: str, scope: CurrentScope, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await repo.find_alert(conn, alert_id, scope, detail=True)
        if row is None:
            raise not_found("alert")
        return await repo.alert_detail(conn, row)


@router.post("/{alert_id}/ack", response_model=AlertSummaryOut)
async def ack_alert(alert_id: str, user: CurrentUser, scope: CurrentScope, pool: Pool) -> dict[str, Any]:
    async def work(conn: asyncpg.Connection, row: asyncpg.Record) -> bool:
        return await actions.ack_alert(conn, row, user)

    changed, summary, row = await _mutate(pool, alert_id, scope, work)
    if changed:
        await _safe_publish(
            "alert.acked",
            {
                "alertId": summary["id"],
                "shortId": summary["shortId"],
                "by": {"kind": "user", "id": str(user.id), "displayName": user.display_name},
                "status": "acknowledged",
            },
            row["team_id"],
        )
    return summary


@router.post("/{alert_id}/resolve", response_model=AlertSummaryOut)
async def resolve_alert(
    alert_id: str,
    user: CurrentUser,
    scope: CurrentScope,
    pool: Pool,
    rabbit: RabbitDep,
    body: ResolveBody | None = None,
) -> dict[str, Any]:
    note = body.resolutionNote if body else None
    outbox_ids: list[int] = []

    async def work(conn: asyncpg.Connection, row: asyncpg.Record) -> bool:
        changed = await actions.resolve_alert(conn, row, user, note)
        if changed:
            outbox_ids.extend(await actions.plan_resolved_updates(conn, row["id"]))
        return changed

    changed, summary, row = await _mutate(pool, alert_id, scope, work)
    if changed:
        await _publish_outbox(pool, rabbit, outbox_ids)
        await _safe_publish(
            "alert.resolved",
            {
                "alertId": summary["id"],
                "shortId": summary["shortId"],
                "by": {"kind": "user", "id": str(user.id), "displayName": user.display_name},
                "status": "resolved",
            },
            row["team_id"],
        )
    return summary


@router.post("/{alert_id}/assign", response_model=AlertSummaryOut)
async def assign_alert(
    alert_id: str, body: AssignBody, user: CurrentUser, scope: CurrentScope, pool: Pool
) -> dict[str, Any]:
    assignee = _uuid_param(body.userId, "userId")
    if assignee is None:
        raise AppError(422, "validation_error", "userId: must be a uuid")

    async def work(conn: asyncpg.Connection, row: asyncpg.Record) -> bool:
        return await actions.assign_alert(conn, row, user, assignee)

    changed, summary, row = await _mutate(pool, alert_id, scope, work)
    if changed:
        await _safe_publish("alert.updated", summary, row["team_id"])
    return summary


@router.post("/{alert_id}/comments", status_code=201, response_model=TimelineOut)
async def comment_alert(
    alert_id: str, body: CommentBody, user: CurrentUser, scope: CurrentScope, pool: Pool
) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise AppError(422, "validation_error", "text: must not be blank")

    async def work(conn: asyncpg.Connection, row: asyncpg.Record) -> dict[str, Any]:
        return await actions.add_comment(conn, row, user, text)

    entry, summary, row = await _mutate(pool, alert_id, scope, work, needs_role=False)
    await _safe_publish("alert.updated", summary, row["team_id"])
    return entry
