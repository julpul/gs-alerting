"""AI suggestion endpoints on alerts: read, accept/reject (audited), resolution note draft."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter
from pydantic import ValidationError

from app.ai.resolution_note import draft_note
from app.api.deps import CurrentScope, CurrentUser, Pool
from app.core.audit import audit, user_actor
from app.core.errors import AppError, ORJSONResponse, forbidden, not_found
from app.core.ids import parse_uuid
from app.core.scope import Scope
from app.core.timeline import add_timeline
from app.repositories import alerts as alerts_repo
from app.repositories import rules as rules_repo
from app.schemas import api as wire

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])

CONTENT_KEYS = (
    "diagnosis",
    "nextStep",
    "nextStepId",
    "confidence",
    "fixedBefore",
    "similarIncidents",
    "sources",
    "draftPullRequestUrl",
    "aiUnavailable",
)
VISIBLE = ("ready", "degraded", "accepted", "rejected")


class SuggestionOut(wire.Suggestion):
    nextStepId: str | None = None
    aiUnavailable: bool | None = None


def suggestion_out(row: asyncpg.Record) -> dict[str, Any]:
    content = row["content"] or {}
    status = row["status"] if row["status"] in ("accepted", "rejected") else "draft"
    data = {k: content[k] for k in CONTENT_KEYS if k in content}
    data.update(id=str(row["id"]), alertId=str(row["alert_id"]), status=status)
    try:
        return SuggestionOut.model_validate(data).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise AppError(500, "internal_error", "stored suggestion has an unexpected shape") from exc


async def _alert(conn: asyncpg.Connection, ref: str, scope: Scope, *, detail: bool = False) -> asyncpg.Record:
    alert = await alerts_repo.find_alert(conn, ref, scope, detail=detail)
    if alert is None:
        raise not_found("alert")
    return alert


@router.get("/{alert_id}/suggestion")
async def get_suggestion(alert_id: str, pool: Pool, scope: CurrentScope) -> ORJSONResponse:
    async with pool.acquire() as conn:
        alert = await _alert(conn, alert_id, scope)
        row = await conn.fetchrow(
            "SELECT id, alert_id, status, content FROM suggestions WHERE alert_id = $1 AND status = ANY($2) "
            "ORDER BY created_at DESC LIMIT 1",
            alert["id"],
            list(VISIBLE),
        )
    if row is None:
        raise not_found("suggestion")
    return ORJSONResponse(suggestion_out(row))


@router.post("/{alert_id}/suggestion/{suggestion_id}/decision")
async def decide_suggestion(
    alert_id: str,
    suggestion_id: str,
    body: wire.Decision,
    pool: Pool,
    user: CurrentUser,
    scope: CurrentScope,
) -> ORJSONResponse:
    sid = parse_uuid(suggestion_id)
    async with pool.acquire() as conn:
        alert = await _alert(conn, alert_id, scope)
        if sid is None:
            raise not_found("suggestion")
        if not await rules_repo.can_edit_team(conn, scope, alert["team_id"]):
            raise forbidden("you cannot decide on suggestions of this team")
        target = "accepted" if body.decision.value == "approve" else "rejected"
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, alert_id, status, content FROM suggestions WHERE id = $1 AND alert_id = $2 "
                "FOR UPDATE",
                sid,
                alert["id"],
            )
            if row is None:
                raise not_found("suggestion")
            if row["status"] in ("accepted", "rejected") and row["status"] != target:
                raise AppError(409, "conflict", f"suggestion was already {row['status']}")
            if row["status"] not in ("accepted", "rejected"):
                await _record(conn, row, alert["id"], user, target, body.comment)
                row = await conn.fetchrow(
                    "SELECT id, alert_id, status, content FROM suggestions WHERE id = $1", sid
                )
    return ORJSONResponse(suggestion_out(row))


async def _record(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    alert_id: UUID,
    user: Any,
    target: str,
    comment: str | None,
) -> None:
    actor = user_actor(user.id, user.display_name)
    await conn.execute("UPDATE suggestions SET status = $2 WHERE id = $1", row["id"], target)
    await add_timeline(
        conn,
        alert_id,
        "suggestion",
        f"AI suggestion {target} by {user.display_name}",
        actor=actor,
        data={"suggestionId": str(row["id"]), "decision": target},
    )
    await audit(
        conn,
        actor,
        f"suggestion.{'accept' if target == 'accepted' else 'reject'}",
        "suggestion",
        str(row["id"]),
        {
            "alertId": str(alert_id),
            "nextStepId": (row["content"] or {}).get("nextStepId"),
            "comment": comment,
        },
    )


@router.get("/{alert_id}/resolution-note-draft")
async def resolution_note_draft(alert_id: str, pool: Pool, scope: CurrentScope) -> ORJSONResponse:
    async with pool.acquire() as conn:
        alert = await _alert(conn, alert_id, scope, detail=True)
        return ORJSONResponse(await draft_note(conn, alert))
