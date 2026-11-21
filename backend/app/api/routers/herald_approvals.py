"""Herald UI: `POST /api/approvals/{id}/decide` (`{approve: bool}` or `{decision: approve|reject}`).

`id` is the approval short id (also accepted: our uuid). Deciding goes through
`app.runbooks.approvals.decide_with_effects`, which runs the approved runbook action.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.api.deps import Pool, RabbitDep
from app.core.errors import AppError
from app.core.outbox import publish_outbox_rows
from app.herald.identity import Actor
from app.herald.routing import herald_router
from app.runbooks import approvals

log = logging.getLogger(__name__)
router = herald_router(prefix="/api/approvals", tags=["herald"])


class DecideBody(BaseModel):
    approve: bool | None = None
    decision: str | None = None
    comment: str | None = Field(default=None, max_length=2000)


def _decision(body: DecideBody) -> str:
    if body.approve is not None:
        return "approve" if body.approve else "reject"
    if body.decision in ("approve", "reject"):
        return body.decision
    raise AppError(422, "validation_error", "say approve: true/false (or decision: approve|reject)")


@router.post("/{approval_id}/decide")
async def decide(
    approval_id: str, body: DecideBody, who: Actor, pool: Pool, rabbit: RabbitDep
) -> dict[str, Any]:
    decision = _decision(body)
    async with pool.acquire() as conn:
        row = await approvals.get_row(conn, approval_id)
        if row is None:
            raise AppError(404, "not_found", "no such approval")
        if row["alert_id"] is not None:
            team_id = await conn.fetchval("SELECT team_id FROM alerts WHERE id = $1", row["alert_id"])
            if team_id is None or not who.scope.can_see_team(team_id):
                raise AppError(404, "not_found", "no such approval")
        result = await approvals.decide_with_effects(
            conn, row["id"], decision=decision, actor=who.actor, comment=body.comment
        )
        run = await conn.fetchrow(
            "SELECT state, output FROM step_runs WHERE approval_id = $1 AND state IN ('succeeded', 'failed') "
            "ORDER BY at DESC, id DESC LIMIT 1",
            row["id"],
        )
    try:
        await publish_outbox_rows(pool, rabbit, result.outbox_ids)
    except Exception:
        log.exception("outbox publish failed; relay will retry")
    status = result.approval["status"]
    outcome = None
    if run is not None:
        ok = run["state"] == "succeeded"
        outcome = {"ok": ok, "message": run["output"] or "", "error": None if ok else run["output"]}
    return {"status": status, "result": outcome, "approval": result.approval["shortId"]}
