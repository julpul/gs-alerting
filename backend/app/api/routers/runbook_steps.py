"""`POST /v1/alerts/{alertId}/runbook/steps/{stepId}/run`: run a step; a risky one needs `?confirmed=true`."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import CurrentScope, CurrentUser, Pool, RabbitDep
from app.core.audit import user_actor
from app.core.errors import not_found
from app.core.outbox import publish_outbox_rows
from app.repositories import alert_actions as actions
from app.repositories import alerts as repo
from app.runbooks import approvals
from app.runbooks.runner import run_step
from app.schemas import api as wire

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/alerts", tags=["runbooks"])


@router.post(
    "/{alert_id}/runbook/steps/{step_id}/run",
    response_model=wire.RunbookStep,
    response_model_exclude_none=True,
)
async def run_runbook_step(
    alert_id: str,
    step_id: str,
    scope: CurrentScope,
    user: CurrentUser,
    pool: Pool,
    rabbit: RabbitDep,
    confirmed: Annotated[bool, Query()] = False,
) -> Any:
    async with pool.acquire() as conn:
        row = await repo.find_alert(conn, alert_id, scope, detail=True)
        if row is None:
            raise not_found("alert")
        await actions.require_actor_role(conn, scope, row)
        await approvals.expire_due(conn)
        outcome = await run_step(
            conn, dict(row), step_id, user_actor(user.id, user.display_name), confirmed=confirmed
        )
    try:
        await publish_outbox_rows(pool, rabbit, outcome.outbox_ids)
    except Exception:
        log.exception("outbox publish failed; relay will retry")
    return outcome.step
