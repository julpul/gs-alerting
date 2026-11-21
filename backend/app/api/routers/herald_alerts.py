"""Herald UI alerts: list, detail, ack / resolve / assign / notes, runbook steps, status update, postmortem.

Every handler resolves the Herald id (short id) through our scope-filtered repository and acts with our
`alert_actions` / runbook runner as the calling person; SSE events are the same as for /v1.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import Query
from pydantic import BaseModel, Field

from app.api.deps import Pool, RabbitDep
from app.core.errors import AppError, not_found
from app.core.outbox import publish_outbox_rows
from app.core.sse import publish_sse
from app.herald import reports
from app.herald.detail import alert_detail
from app.herald.identity import Actor, HeraldActor
from app.herald.ids import (
    our_statuses,
    people_refs,
    short_from_ref,
    source_id_by_name,
    team_for_owner,
    user_for_person,
)
from app.herald.queries import herald_alerts
from app.herald.routing import herald_router
from app.repositories import alert_actions as actions
from app.repositories import alerts as repo
from app.runbooks import approvals
from app.runbooks.lookup import runbook_for_alert
from app.runbooks.runner import run_step as run

log = logging.getLogger(__name__)
router = herald_router(prefix="/api/alerts", tags=["herald"])

MAX_LIMIT = 500
AI_NOTE_TIMEOUT_S = 8.0


class ResolveBody(BaseModel):
    note: str | None = Field(default=None, max_length=10_000)
    resolutionNote: str | None = Field(default=None, max_length=10_000)


class AssignBody(BaseModel):
    to: str | None = None


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


def _ref(alert_id: str) -> str:
    short = short_from_ref(alert_id)
    if short is None:
        raise not_found("alert")
    return f"A-{short}"


async def _find(conn: asyncpg.Connection, alert_id: str, who: HeraldActor, **kw: Any) -> asyncpg.Record:
    row = await repo.find_alert(conn, _ref(alert_id), who.scope, **kw)
    if row is None:
        raise AppError(404, "not_found", "no such alert")  # same answer whether missing or not yours
    return row


async def _publish(event: str, data: dict[str, Any], team_id: UUID) -> None:
    try:
        await publish_sse(event, data, team_ids=[team_id])
    except Exception:
        log.exception("SSE publish of %s failed", event)


async def _mutate(
    pool: asyncpg.Pool,
    alert_id: str,
    who: HeraldActor,
    work: Callable[[asyncpg.Connection, asyncpg.Record], Awaitable[Any]],
    *,
    needs_role: bool = True,
) -> tuple[Any, dict[str, Any], asyncpg.Record]:
    async with pool.acquire() as conn, conn.transaction():
        row = await _find(conn, alert_id, who, for_update=True)
        if needs_role:
            await actions.require_actor_role(conn, who.scope, row)
        result = await work(conn, row)
        fresh = await repo.find_alert(conn, str(row["id"]), who.scope)
    return result, repo.summary_out(fresh), row


def _by(who: HeraldActor) -> dict[str, Any]:
    return {"kind": "user", "id": str(who.user_id), "displayName": who.user["display_name"]}


@router.get("")
async def list_alerts(
    who: Actor,
    pool: Pool,
    owner: str | None = None,
    status: str | None = None,
    source: str | None = None,
    severity: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
) -> list[dict[str, Any]]:
    """Herald order: most recently seen first; every status unless `status` is given."""
    async with pool.acquire() as conn:
        team_id = assignee = None
        if owner and owner.startswith("person:"):
            person = await user_for_person(conn, owner)
            if person is None:
                return []
            assignee = person["id"]
        elif owner:
            team_id = await team_for_owner(conn, owner, who.scope)
        source_id = await source_id_by_name(conn, source) if source else None
        if source and source_id is None:
            return []
        ids: list[UUID] = []
        cursor = None
        while len(ids) < limit:
            page, cursor = await repo.list_alerts(
                conn,
                who.scope,
                statuses=our_statuses(status),
                severities=[severity] if severity else None,
                team_id=team_id,
                source_id=source_id,
                cursor=cursor,
                limit=repo.MAX_LIMIT,
            )
            ids += [
                UUID(a["id"])
                for a in page
                if assignee is None or (a["assignee"] or {}).get("id") == str(assignee)
            ]
            if cursor is None:
                break
        return await herald_alerts(conn, ids[:limit])


@router.get("/{alert_id}")
async def get_alert(alert_id: str, who: Actor, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await _find(conn, alert_id, who, detail=True)
        return await alert_detail(conn, row)


@router.post("/{alert_id}/ack")
async def ack(alert_id: str, who: Actor, pool: Pool) -> dict[str, Any]:
    async def work(conn: asyncpg.Connection, row: asyncpg.Record) -> bool:
        return await actions.ack_alert(conn, row, who.model)

    changed, summary, row = await _mutate(pool, alert_id, who, work)
    if not changed:
        raise AppError(409, "conflict", "alert is already acked")
    await _publish(
        "alert.acked",
        {"alertId": summary["id"], "shortId": summary["shortId"], "by": _by(who), "status": "acknowledged"},
        row["team_id"],
    )
    return {"status": "acked"}


@router.post("/{alert_id}/resolve")
async def resolve(
    alert_id: str, who: Actor, pool: Pool, rabbit: RabbitDep, body: ResolveBody | None = None
) -> dict[str, Any]:
    note = (body.note or body.resolutionNote) if body else None
    outbox_ids: list[int] = []

    async def work(conn: asyncpg.Connection, row: asyncpg.Record) -> bool:
        changed = await actions.resolve_alert(conn, row, who.model, note)
        if changed:
            outbox_ids.extend(await actions.plan_resolved_updates(conn, row["id"]))
        return changed

    changed, summary, row = await _mutate(pool, alert_id, who, work)
    if not changed:
        raise AppError(409, "conflict", "alert is already resolved")
    try:
        await publish_outbox_rows(pool, rabbit, outbox_ids)
    except Exception:
        log.exception("outbox publish failed; relay will retry")
    await _publish(
        "alert.resolved",
        {"alertId": summary["id"], "shortId": summary["shortId"], "by": _by(who), "status": "resolved"},
        row["team_id"],
    )
    return {"status": "resolved"}


@router.post("/{alert_id}/assign")
async def assign(alert_id: str, body: AssignBody, who: Actor, pool: Pool) -> dict[str, Any]:
    """Hand the alert to a person (`person:<id>`); `to: null` takes it back (unassigned)."""
    if not body.to:

        async def release(conn: asyncpg.Connection, row: asyncpg.Record) -> bool:
            return await actions.unassign_alert(conn, row, who.model)

        changed, summary, row = await _mutate(pool, alert_id, who, release)
        if changed:
            await _publish("alert.updated", summary, row["team_id"])
        return {"assignee": None}
    async with pool.acquire() as conn:
        person = await user_for_person(conn, body.to) if body.to.startswith("person:") else None
    if person is None:
        raise AppError(422, "validation_error", "choose a person to hand this to")

    async def work(conn: asyncpg.Connection, row: asyncpg.Record) -> bool:
        return await actions.assign_alert(conn, row, who.model, person["id"])

    changed, summary, row = await _mutate(pool, alert_id, who, work)
    if changed:
        await _publish("alert.updated", summary, row["team_id"])
    return {"assignee": body.to}


@router.post("/{alert_id}/notes")
async def add_note(alert_id: str, body: NoteBody, who: Actor, pool: Pool) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise AppError(422, "validation_error", "text: must not be blank")

    async def work(conn: asyncpg.Connection, row: asyncpg.Record) -> dict[str, Any]:
        return await actions.add_comment(conn, row, who.model, text)

    _, summary, row = await _mutate(pool, alert_id, who, work, needs_role=False)
    await _publish("alert.updated", summary, row["team_id"])
    return {"saved": True}


@router.post("/{alert_id}/steps/{step_id}/run")
async def run_step(alert_id: str, step_id: str, who: Actor, pool: Pool, rabbit: RabbitDep) -> dict[str, Any]:
    """Tick a manual step, run a safe action, or ask for approval (Herald response shapes)."""
    async with pool.acquire() as conn:
        row = await _find(conn, alert_id, who, detail=True)
        await actions.require_actor_role(conn, who.scope, row)
        await approvals.expire_due(conn)
        view = await runbook_for_alert(conn, dict(row))
        step = next(
            (s for s in (view.steps if view else []) if str(s.order) == step_id or s.id == step_id), None
        )
        if step is None:
            raise AppError(404, "not_found", "no such step")
        outcome = await run(conn, dict(row), step.id, who.actor)
        approver = None
        if outcome.approval:
            approver = await _approver_ref(conn, outcome.approval, who)
    try:
        await publish_outbox_rows(pool, rabbit, outcome.outbox_ids)
    except Exception:
        log.exception("outbox publish failed; relay will retry")
    state = outcome.step.state.value if outcome.step.state else "not_started"
    if outcome.approval:
        return {"state": "awaiting_approval", "approval": outcome.approval["shortId"], "approver": approver}
    if not outcome.step.action:
        return {"state": "done"}
    output = outcome.step.lastRun.output if outcome.step.lastRun else ""
    ok = state == "succeeded"
    return {
        "state": "done" if ok else "failed",
        "result": {"ok": ok, "message": output, "error": None if ok else output},
    }


async def _approver_ref(conn: asyncpg.Connection, approval: dict[str, Any], who: HeraldActor) -> str | None:
    ids = [UUID(a["id"]) for a in approval.get("approvers") or []]
    refs = await people_refs(conn, ids)
    picks = [refs[i] for i in ids if i in refs]
    return next((p for p in picks if p != who.ref), picks[0] if picks else None)


async def _ai_summary(conn: asyncpg.Connection, row: asyncpg.Record) -> str | None:
    """Resolution-note draft from the timeline (LLM when configured); never fails the request."""
    try:
        from app.ai.resolution_note import draft_note

        draft = await asyncio.wait_for(draft_note(conn, row), AI_NOTE_TIMEOUT_S)
    except Exception:
        log.info("no AI summary for the postmortem draft", exc_info=True)
        return None
    if draft.get("aiUnavailable"):
        return None
    return draft.get("note") or None


async def _names(conn: asyncpg.Connection, refs: set[str | None]) -> dict[str, str]:
    """Display names of the people an alert mentions (`person:<id>` refs)."""
    ids = [r.removeprefix("person:") for r in refs if r and r.startswith("person:")]
    rows = await conn.fetch(
        "SELECT email, display_name FROM users WHERE split_part(email, '@', 1) = ANY($1)", ids
    )
    return {f"person:{r['email'].split('@', 1)[0]}": r["display_name"] for r in rows}


@router.get("/{alert_id}/status-update")
async def status_update(alert_id: str, who: Actor, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        detail = await alert_detail(conn, await _find(conn, alert_id, who, detail=True))
        names = await _names(conn, {detail["incident_manager"]})
    return {"text": reports.status_update(detail, datetime.now(UTC), names)}


@router.get("/{alert_id}/postmortem")
async def postmortem(alert_id: str, who: Actor, pool: Pool, names: bool = False) -> dict[str, Any]:
    """Blameless draft: roles, not names, unless `?names=true`."""
    async with pool.acquire() as conn:
        row = await _find(conn, alert_id, who, detail=True)
        detail = await alert_detail(conn, row)
        lead = await conn.fetchval(
            "SELECT split_part(u.email, '@', 1) FROM team_domains td JOIN role_assignments ra "
            "ON ra.role = 'domain_root' AND ra.scope_type = 'domain' AND ra.scope_id = td.domain_id "
            "JOIN users u ON u.id = ra.user_id WHERE td.team_id = $1 ORDER BY u.email LIMIT 1",
            row["team_id"],
        )
        notes = [
            r["note"]
            for r in await conn.fetch(
                "SELECT note FROM resolutions WHERE alert_id = $1 ORDER BY created_at", row["id"]
            )
        ]
        summary = await _ai_summary(conn, row) if detail["status"] == "resolved" else None
        people = await _names(conn, {t["actor"] for t in detail["timeline"]}) if names else None
    markdown = reports.postmortem(detail, lead=lead, names=people, resolution_notes=notes, ai_summary=summary)
    guide = reports.SEVERITY_GUIDE.get(detail["severity"], {})
    return {"markdown": markdown, "required": bool(guide.get("postmortem"))}
