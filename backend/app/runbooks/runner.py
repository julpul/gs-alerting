"""Runs runbook steps (spec 11.3 / 11.4).

A step runs immediately when it is an action with `approval: none`, the risk is not high and the trust level
of (domain, action) is `auto`. Everything else becomes an approval (`request_approval`); the action runs when
`approvals.decide` approves it (`finish_approval`). Every outcome is a `step_runs` row, a timeline entry and
an audit entry; SSE `alert.updated` follows.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import ValidationError

from app.core.audit import audit
from app.core.errors import AppError, not_found
from app.core.ids import uuid7
from app.core.sse import publish_sse
from app.core.timeline import add_timeline
from app.pipeline.fields import EventView
from app.repositories import alerts as alerts_repo
from app.runbooks.actions.registry import Action, ActionContext, ActionResult, get_action
from app.runbooks.approvals import approvals_wire, get_row, request_approval, stored_actor
from app.runbooks.lookup import (
    build_event_view,
    find_runbook,
    render_runbook,
    render_value,
    step_states,
)
from app.runbooks.trust import get_trust, record_outcome
from app.schemas import api as wire

log = logging.getLogger(__name__)

ACTION_TIMEOUT_S = 30.0
OUTPUT_LIMIT = 4000


@dataclass
class RunOutcome:
    step: wire.RunbookStep
    approval: dict[str, Any] | None = None
    outbox_ids: list[int] = field(default_factory=list)


def _validate(spec: Action, params: dict[str, Any]) -> Any:
    try:
        return spec.params_schema.model_validate(params)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise AppError(422, "validation_error", f"parameters.{loc}: {first['msg']}") from exc


async def _record(
    conn: asyncpg.Connection,
    alert_id: UUID,
    step_id: str,
    state: str,
    actor: dict[str, Any],
    output: str,
    *,
    approval_id: UUID | None = None,
    text: str | None = None,
) -> None:
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO step_runs (id, alert_id, step_id, state, approval_id, output, run_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid7(),
            alert_id,
            step_id,
            state,
            approval_id,
            output[:OUTPUT_LIMIT],
            actor,
        )
        await add_timeline(
            conn,
            alert_id,
            "step_run",
            text or f"Step {step_id}: {state}",
            actor=actor,
            data={"stepId": step_id, "state": state, "approvalId": str(approval_id) if approval_id else None},
        )
        await audit(conn, actor, f"runbook.step.{state}", "alert", str(alert_id), {"stepId": step_id})


async def publish_alert_updated(conn: asyncpg.Connection, alert_id: UUID) -> None:
    row = await conn.fetchrow(f"{alerts_repo.SUMMARY_SELECT} WHERE a.id = $1", alert_id)
    if row is None:
        return
    try:
        await publish_sse("alert.updated", alerts_repo.summary_out(row), team_ids=[row["team_id"]])
    except Exception:
        log.exception("SSE publish of alert.updated failed")


async def _execute(
    conn: asyncpg.Connection,
    alert: dict[str, Any],
    ev: EventView,
    action: str,
    params: dict[str, Any],
    step_id: str,
    actor: dict[str, Any],
) -> tuple[ActionResult, list[int]]:
    spec = get_action(action)
    if spec is None:
        return ActionResult(False, f"unknown action {action!r}"), []
    ctx = ActionContext(alert=alert, ev=ev, actor=actor, step_id=step_id, run_id=uuid7(), conn=conn)
    try:
        parsed = spec.params_schema.model_validate(params)
        result = await asyncio.wait_for(spec.run(parsed, ctx), ACTION_TIMEOUT_S)
    except ValidationError as exc:
        result = ActionResult(False, f"invalid parameters: {exc.errors()[0]['msg']}")
    except Exception as exc:
        log.warning("action %s failed", action, exc_info=True)
        result = ActionResult(False, f"{type(exc).__name__}: {exc}")
    return result, ctx.outbox_ids


async def _finish(
    conn: asyncpg.Connection,
    alert: dict[str, Any],
    step_id: str,
    action: str,
    result: ActionResult,
    actor: dict[str, Any],
    *,
    approved: bool,
    approval_id: UUID | None,
    suffix: str = "",
) -> None:
    state = "succeeded" if result.ok else "failed"
    await _record(
        conn,
        alert["id"],
        step_id,
        state,
        actor,
        result.output,
        approval_id=approval_id,
        text=f"{actor['name']} ran {action}{suffix}: {state}. {result.output[:200]}",
    )
    await record_outcome(conn, alert["domain_id"], action, success=result.ok, approved=approved)


async def step_view(conn: asyncpg.Connection, alert: dict[str, Any], step_id: str) -> wire.RunbookStep:
    ev = await build_event_view(conn, alert)
    row = await find_runbook(conn, alert, ev)
    if row is None:
        raise not_found("runbook")
    view = await render_runbook(conn, row, alert, ev)
    step = next((s for s in view.steps if s.id == step_id), None)
    if step is None:
        raise not_found("runbook step")
    return step


async def run_step(
    conn: asyncpg.Connection, alert: dict[str, Any], step_id: str, actor: dict[str, Any]
) -> RunOutcome:
    """Run (or request approval for) one step of the alert's runbook. `actor` is the audited actor dict."""
    actor = stored_actor(actor)
    ev = await build_event_view(conn, alert)
    row = await find_runbook(conn, alert, ev)
    if row is None:
        raise not_found("runbook")
    stored = next((s for s in row["steps"] or [] if s.get("id") == step_id), None)
    if stored is None:
        raise not_found("runbook step")

    latest = (await step_states(conn, alert["id"])).get(step_id)
    if latest and latest["state"] == "pending_approval" and latest["approval_id"]:
        pending = await get_row(conn, latest["approval_id"])
        if pending is not None and pending["status"] == "pending":
            (wire_approval,) = await approvals_wire(conn, [pending])
            return RunOutcome(await step_view(conn, alert, step_id), wire_approval)

    action = stored.get("action")
    text = (stored.get("text") or step_id)[:200]
    params = render_value(stored.get("parameters") or {}, ev)
    outbox_ids: list[int] = []
    approval: dict[str, Any] | None = None

    if not action:
        await _record(
            conn,
            alert["id"],
            step_id,
            "succeeded",
            actor,
            "marked done",
            text=f"{actor['name']} completed step {step_id}: {text}",
        )
    else:
        spec = get_action(action)
        if spec is None:
            raise AppError(422, "unknown_action", f"action {action!r} is not registered")
        _validate(spec, params)
        risk = stored.get("risk") or spec.default_risk
        trust = await get_trust(conn, alert["domain_id"], action)
        if stored.get("approval") == "required" or risk == "high" or trust.level != "auto":
            approval = await request_approval(
                conn,
                origin="runbook_step",
                action=action,
                parameters=params,
                reason=f"Runbook step {step_id}: {text}",
                risk=risk,
                alert_id=alert["id"],
                requested_by=actor,
            )
            await _record(
                conn,
                alert["id"],
                step_id,
                "pending_approval",
                actor,
                f"waiting for approval {approval['id']}",
                approval_id=UUID(approval["id"]),
                text=f"{actor['name']} requested approval to run {action} ({risk} risk)",
            )
        else:
            result, outbox_ids = await _execute(conn, alert, ev, action, params, step_id, actor)
            await _finish(conn, alert, step_id, action, result, actor, approved=False, approval_id=None)
    await publish_alert_updated(conn, alert["id"])
    return RunOutcome(await step_view(conn, alert, step_id), approval, outbox_ids)


async def finish_approval(
    conn: asyncpg.Connection, approval: asyncpg.Record, decided_by: dict[str, Any]
) -> list[int]:
    """After a decision on a `runbook_step` approval: run the approved action, or record the rejection."""
    sr = await conn.fetchrow(
        "SELECT alert_id, step_id, run_by FROM step_runs WHERE approval_id = $1 "
        "ORDER BY at DESC, id DESC LIMIT 1",
        approval["id"],
    )
    alert_row = (
        await conn.fetchrow("SELECT * FROM alerts WHERE id = $1", approval["alert_id"])
        if approval["alert_id"]
        else None
    )
    if sr is None or alert_row is None:
        return []
    alert = dict(alert_row)
    actor = stored_actor(sr["run_by"])
    step_id, action = sr["step_id"], approval["action"]
    outbox_ids: list[int] = []
    if approval["status"] != "approved":
        await _record(
            conn,
            alert["id"],
            step_id,
            "skipped",
            actor,
            f"rejected by {decided_by['name']}" + (f": {approval['comment']}" if approval["comment"] else ""),
            approval_id=approval["id"],
            text=f"{decided_by['name']} rejected {action}; step skipped",
        )
    else:
        ev = await build_event_view(conn, alert)
        result, outbox_ids = await _execute(
            conn, alert, ev, action, approval["parameters"] or {}, step_id, actor
        )
        await _finish(
            conn,
            alert,
            step_id,
            action,
            result,
            actor,
            approved=True,
            approval_id=approval["id"],
            suffix=f" (approved by {decided_by['name']})",
        )
    await publish_alert_updated(conn, alert["id"])
    return outbox_ids
