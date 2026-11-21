"""Herald `AlertDetail` for one alert: our `alert_detail` + runbook / step state / approvals / why."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg

from app.herald import views
from app.herald.codebase import codebase_bundle
from app.herald.ids import actor_user_ids, people_refs
from app.herald.queries import herald_alerts
from app.repositories import alerts as repo
from app.schemas import api as wire

log = logging.getLogger(__name__)


async def _why(conn: asyncpg.Connection, row: asyncpg.Record) -> dict[str, Any] | None:
    if row["rule_id"] is None:
        return None
    match = await conn.fetchval("SELECT definition->'match' FROM rules WHERE id = $1", row["rule_id"])
    if not match:
        return None
    try:
        from app.runbooks.lookup import build_event_view

        return views.explain(match, await build_event_view(conn, dict(row)))
    except Exception:
        log.exception("could not explain rule match for alert %s", row["id"])
        return None


async def _approval_steps(conn: asyncpg.Connection, alert_id: UUID) -> dict[UUID, str]:
    rows = await conn.fetch(
        "SELECT DISTINCT ON (approval_id) approval_id, step_id FROM step_runs "
        "WHERE alert_id = $1 AND approval_id IS NOT NULL ORDER BY approval_id, at",
        alert_id,
    )
    return {r["approval_id"]: r["step_id"] for r in rows}


async def alert_detail(conn: asyncpg.Connection, row: asyncpg.Record) -> dict[str, Any]:
    """`row` from `repositories.alerts.find_alert(..., detail=True)` (already scope-checked)."""
    ours = await repo.alert_detail(conn, row)
    (out,) = await herald_alerts(conn, [row["id"]])
    runbook = wire.Runbook.model_validate(ours["runbook"]) if ours.get("runbook") else None
    steps = {s.id: {"id": s.order, "action": s.action} for s in (runbook.steps or [])} if runbook else {}

    approval_rows = await conn.fetch(
        "SELECT * FROM approvals WHERE alert_id = $1 ORDER BY created_at, id", row["id"]
    )
    by_uuid = {str(a["id"]): a for a in approval_rows}
    step_of = await _approval_steps(conn, row["id"])
    users = {u for a in approval_rows for u in a["approver_user_ids"]}
    users |= actor_user_ids(a["requested_by"] for a in approval_rows)
    users |= actor_user_ids(a["decided_by"] for a in approval_rows)
    users |= actor_user_ids(t["actor"] for t in ours["timeline"])
    users |= {UUID(t["data"]["assigneeId"]) for t in ours["timeline"] if (t["data"] or {}).get("assigneeId")}
    if runbook:
        users |= {UUID(s.lastRun.by.id) for s in runbook.steps or [] if s.lastRun and _is_uuid(s.lastRun.by)}
    refs = await people_refs(conn, users)

    timeline = [views.timeline_out(t, refs, steps, by_uuid) for t in ours["timeline"]]
    out.update(
        await codebase_bundle(conn, row, ours),
        sample_event=row["raw_event"],
        runbook=views.runbook_out(runbook) if runbook else None,
        step_state=views.step_state_out(runbook, refs, by_uuid) if runbook else {},
        why=await _why(conn, row),
        timeline=[t for t in timeline if t is not None],
        approvals=[
            views.approval_out(a, refs, steps.get(step_of.get(a["id"], ""), {}).get("id"))
            for a in approval_rows
        ],
        suggestion=ours.get("suggestion"),
        rule_name=row["rule_name"],
        team_name=row["team_name"],
        next_renotify_at=views.iso(row["next_renotify_at"]),
    )
    return out


def _is_uuid(actor: Any) -> bool:
    try:
        UUID(str(actor.id))
    except (ValueError, AttributeError):
        return False
    return True
