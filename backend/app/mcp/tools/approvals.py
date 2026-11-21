"""Approval tools: an agent asks a human for permission and long-polls for the decision (spec 11.4, 14)."""

import logging
import time
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from pydantic import Field

from app.core.errors import AppError, not_found
from app.core.outbox import publish_outbox_rows
from app.core.redis import rkey
from app.core.scope import Scope
from app.mcp.context import ToolContext
from app.mcp.registry import tool
from app.repositories import alert_actions as actions
from app.repositories import alerts as repo
from app.runbooks import approvals
from app.runbooks.runner import run_step

log = logging.getLogger(__name__)

DEFAULT_WAIT_S = 120
MAX_WAIT_S = 300
DB_RECHECK_S = 5.0
RISKS = {"low", "medium", "high"}


def decision_result(row: asyncpg.Record) -> dict[str, Any]:
    decided = row["decided_by"] or {}
    return {
        "status": row["status"],
        "approvalId": str(row["id"]),
        "decidedBy": decided.get("name"),
        "comment": row["comment"],
    }


def _may_see(row: asyncpg.Record, scope: Scope, alert_team: UUID | None) -> bool:
    requested_by = row["requested_by"] or {}
    return (
        scope.is_master
        or scope.user_id in row["approver_user_ids"]
        or str(scope.user_id) in (requested_by.get("id"), requested_by.get("userId"))
        or (alert_team is not None and scope.can_see_team(alert_team))
    )


async def _visible_approval(conn: asyncpg.Connection, tc: ToolContext, ref: str) -> asyncpg.Record:
    await approvals.expire_due(conn)
    row = await approvals.get_row(conn, ref)
    team = await conn.fetchval("SELECT team_id FROM alerts WHERE id = $1", row["alert_id"]) if row else None
    if row is None or not _may_see(row, tc.scope, team):
        raise not_found("approval")
    return row


async def _wait_for_decision(
    tc: ToolContext, approval_id: UUID, wait_seconds: float
) -> asyncpg.Record | None:
    """Subscribe first, then read the row: a decision between create and subscribe cannot be missed."""
    pubsub = tc.redis.pubsub()
    await pubsub.subscribe(rkey("appr", "wait", str(approval_id)))
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            async with tc.pool.acquire() as conn:
                await approvals.expire_due(conn)
                row = await approvals.get_row(conn, approval_id)
            if row is None or row["status"] != "pending" or time.monotonic() >= deadline:
                return row
            wait = min(DB_RECHECK_S, deadline - time.monotonic())
            await pubsub.get_message(ignore_subscribe_messages=True, timeout=max(wait, 0.05))
    finally:
        try:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        except Exception:
            log.debug("closing appr:wait subscription failed", exc_info=True)


@tool(writes=True)
async def request_approval(
    tc: ToolContext,
    action: Annotated[str, Field(description="what you want to do, e.g. terraform_apply or restart_service")],
    parameters: dict[str, Any],
    reason: str,
    risk: Annotated[str, Field(description="low | medium | high")] = "medium",
    alert_id: str | None = None,
    timeout_seconds: Annotated[
        int, Field(ge=0, le=MAX_WAIT_S, description="how long to wait (0 = don't)")
    ] = (DEFAULT_WAIT_S),
) -> dict:
    """Ask the on-call humans to approve an action, then wait for their decision.

    Approvers are notified (SMS/email "Reply APPROVE <n>", UI inbox). Returns `{status: approved | rejected |
    expired, decidedBy, comment, approvalId}`; when nobody decided within `timeout_seconds` it returns
    `{status: "pending", approvalId}`: poll with `get_approval_status`. Never run the action unless approved.
    """
    if risk not in RISKS:
        raise AppError(422, "validation_error", f"risk: must be one of {', '.join(sorted(RISKS))}")
    if not action.strip() or not reason.strip():
        raise AppError(422, "validation_error", "action and reason must not be blank")
    async with tc.pool.acquire() as conn:
        alert_uuid = None
        if alert_id:
            alert = await repo.find_alert(conn, alert_id, tc.scope)
            if alert is None:
                raise not_found("alert")
            alert_uuid = alert["id"]
        created = await approvals.request_approval(
            conn,
            origin="agent_request",
            action=action.strip(),
            parameters=parameters,
            reason=reason.strip(),
            risk=risk,
            alert_id=alert_uuid,
            requested_by=tc.actor,
        )
    approval_id = UUID(created["id"])
    if timeout_seconds <= 0:
        return {"status": "pending", "approvalId": created["id"]}
    row = await _wait_for_decision(tc, approval_id, min(timeout_seconds, MAX_WAIT_S))
    if row is None or row["status"] == "pending":
        return {"status": "pending", "approvalId": created["id"]}
    return decision_result(row)


@tool()
async def get_approval_status(tc: ToolContext, approval_id: str) -> dict:
    """Current state of an approval you requested (or can see): pending, approved, rejected or expired."""
    async with tc.pool.acquire() as conn:
        row = await _visible_approval(conn, tc, approval_id)
    return {**decision_result(row), "action": row["action"], "risk": row["risk"]}


@tool(writes=True)
async def run_runbook_step(
    tc: ToolContext, alert_id: str, step_id: Annotated[str, Field(description="step id from get_alert")]
) -> dict:
    """Run one runbook step of an alert: executes at once when it is low risk and trusted, otherwise an
    approval is requested from the on-call humans (returned as `approval`)."""
    async with tc.pool.acquire() as conn:
        row = await repo.find_alert(conn, alert_id, tc.scope, detail=True)
        if row is None:
            raise not_found("alert")
        await actions.require_actor_role(conn, tc.scope, row)
        await approvals.expire_due(conn)
        outcome = await run_step(conn, dict(row), step_id, tc.actor)
    try:
        await publish_outbox_rows(tc.pool, tc.rabbit, outcome.outbox_ids)
    except Exception:
        log.exception("outbox publish failed; relay will retry")
    return {
        "step": outcome.step.model_dump(mode="json", exclude_none=True),
        "approval": outcome.approval,
    }
