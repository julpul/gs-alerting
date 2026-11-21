"""Approvals inbox and decisions (spec 11.4). SMS replies and MCP reuse `app.runbooks.approvals.decide`."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import CurrentScope, CurrentUser, Pool, RabbitDep
from app.core.audit import user_actor
from app.core.errors import AppError, not_found
from app.core.outbox import publish_outbox_rows
from app.runbooks import approvals
from app.schemas import api as wire

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/approvals", tags=["approvals"])

STATUSES = {s.value for s in wire.Status2}


@router.get("", response_model=list[wire.Approval])
async def list_approvals(
    scope: CurrentScope, pool: Pool, status: Annotated[str | None, Query()] = None
) -> list[dict[str, Any]]:
    if status is not None and status not in STATUSES:
        raise AppError(422, "validation_error", f"status: must be one of {', '.join(sorted(STATUSES))}")
    async with pool.acquire() as conn:
        return await approvals.list_approvals(conn, scope, status)


@router.post("/{approval_id}/decision", response_model=wire.Approval)
async def decide(
    approval_id: str, body: wire.Decision, user: CurrentUser, pool: Pool, rabbit: RabbitDep
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        if await approvals.get_row(conn, approval_id) is None:
            raise not_found("approval")
        result = await approvals.decide_with_effects(
            conn,
            approval_id,
            decision=body.decision.value,
            actor=user_actor(user.id, user.display_name),
            comment=body.comment,
        )
    try:
        await publish_outbox_rows(pool, rabbit, result.outbox_ids)
    except Exception:
        log.exception("outbox publish failed; relay will retry")
    return result.approval
