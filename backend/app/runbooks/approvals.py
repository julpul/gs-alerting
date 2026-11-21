"""Approvals (spec 11.4): created for risky runbook steps and for agent requests; decided in the UI, by SMS
reply (`APPROVE <shortId>`) or over MCP. Every decision is audited and wakes a waiting MCP long-poll.

Public interface (plan 3.5): `request_approval`, `decide`. `decide_with_effects` additionally returns the
outbox rows (escalation deliveries) that the caller should publish.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
import orjson

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import AppError, forbidden, not_found
from app.core.ids import format_short_id, parse_uuid, uuid7
from app.core.redis import get_redis, rkey
from app.core.scope import Scope, load_scope
from app.core.sse import publish_sse
from app.core.timeline import add_timeline
from app.delivery.channels.base import ChannelError, Message
from app.delivery.channels.registry import get_adapter
from app.delivery.recipients import Person, domain_roots, masters, users_by_ids
from app.escalation.oncall import current_oncall

log = logging.getLogger(__name__)

TTL_SECONDS = 15 * 60
SEND_TIMEOUT_S = 3.0
RISKS = ("low", "medium", "high")
ORIGINS = ("runbook_step", "agent_request")
SMS_LIMIT = 160


def stored_actor(actor: dict[str, Any] | None) -> dict[str, Any]:
    """Either actor spelling (`type/name` as audited, `kind/displayName` as on the wire) -> audited form."""
    actor = actor or {}
    return {
        "type": actor.get("type") or actor.get("kind") or "system",
        "id": str(actor["id"]) if actor.get("id") else None,
        "name": actor.get("name") or actor.get("displayName") or "system",
        "onBehalfOf": actor.get("onBehalfOf"),
        **({"userId": str(actor["userId"])} if actor.get("userId") else {}),
    }


def actor_wire(actor: dict[str, Any] | None) -> dict[str, Any]:
    a = stored_actor(actor)
    return {
        "kind": a["type"],
        "id": a["id"] or "system",
        "displayName": a["name"],
        "onBehalfOf": a["onBehalfOf"],
    }


async def approvers_for(
    conn: asyncpg.Connection, alert: asyncpg.Record | None, requested_by: dict[str, Any]
) -> list[UUID]:
    """On-call of the alert's team plus the Domain Roots of its domains; for requests without an alert the
    roots of the requester's domains (masters if there are none)."""
    people: dict[UUID, Person] = {}
    if alert is not None:
        for uid in await current_oncall(conn, alert["team_id"]):
            people.setdefault(uid, Person(uid, "", ""))
        rows = await conn.fetch("SELECT domain_id FROM team_domains WHERE team_id = $1", alert["team_id"])
        domains = {r["domain_id"] for r in rows} | {alert["domain_id"]}
    else:
        requester = parse_uuid(requested_by.get("userId") or requested_by.get("id") or "")
        domains = set((await load_scope(conn, requester)).domain_ids) if requester else set()
    for domain_id in domains:
        for person in await domain_roots(conn, domain_id):
            people.setdefault(person.id, person)
    if not people:
        for person in await masters(conn):
            people.setdefault(person.id, person)
    return list(people)


async def _users(conn: asyncpg.Connection, ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    if not ids:
        return {}
    rows = await conn.fetch("SELECT id, display_name FROM users WHERE id = ANY($1)", ids)
    return {
        r["id"]: {"kind": "user", "id": str(r["id"]), "displayName": r["display_name"], "onBehalfOf": None}
        for r in rows
    }


async def approvals_wire(conn: asyncpg.Connection, rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    users = await _users(conn, sorted({u for r in rows for u in r["approver_user_ids"]}))
    out = []
    for r in rows:
        out.append(
            {
                "id": str(r["id"]),
                "shortId": r["short_id"] if "short_id" in r.keys() else None,
                "status": r["status"],
                "origin": r["origin"],
                "requestedBy": actor_wire(r["requested_by"]),
                "alertId": str(r["alert_id"]) if r["alert_id"] else None,
                "action": r["action"],
                "parameters": r["parameters"] or {},
                "risk": r["risk"] or "medium",
                "reason": r["reason"] or "",
                "approvers": [users[u] for u in r["approver_user_ids"] if u in users],
                "decidedBy": actor_wire(r["decided_by"]) if r["decided_by"] else None,
                "comment": r["comment"],
                "expiresAt": r["expires_at"],
                "createdAt": r["created_at"],
            }
        )
    return out


async def get_row(conn: asyncpg.Connection, ref: str | UUID, *, lock: bool = False) -> asyncpg.Record | None:
    """By uuid, or by the numeric short id used in SMS replies."""
    suffix = " FOR UPDATE" if lock else ""
    uid = ref if isinstance(ref, UUID) else parse_uuid(str(ref))
    if uid is not None:
        return await conn.fetchrow(f"SELECT * FROM approvals WHERE id = $1{suffix}", uid)
    if str(ref).isdigit():
        try:
            return await conn.fetchrow(f"SELECT * FROM approvals WHERE short_id = $1{suffix}", int(ref))
        except asyncpg.UndefinedColumnError:
            return None
    return None


def _notification_text(row: asyncpg.Record, alert: asyncpg.Record | None) -> tuple[str, str]:
    sid = row["short_id"] if "short_id" in row.keys() else str(row["id"])[:8]
    what = f"{row['action']} ({row['risk']})"
    where = f" for {format_short_id(alert['short_id'])}" if alert else ""
    base = get_settings().public_base_url.rstrip("/")
    link = f"{base}/a/{alert['short_id']}" if alert else f"{base}/approvals"
    tail = f" Reply APPROVE {sid} or REJECT {sid}. {link}"
    head = f"Approval needed: {what}{where}."
    room = SMS_LIMIT - len(head) - len(tail) - 1
    reason = " ".join((row["reason"] or "").split())
    if room > 12 and reason:
        head += " " + (reason if len(reason) <= room else reason[: room - 1] + "…")
    sms = head + tail
    email = (
        f"{what}{where}\n\nReason: {row['reason'] or '-'}\nParameters: "
        f"{orjson.dumps(row['parameters']).decode()}\n\nReply APPROVE {sid} or REJECT {sid} by SMS, "
        f"or decide here: {link}\nExpires {row['expires_at'].isoformat(timespec='seconds')}."
    )
    return sms, email


async def _send(
    conn: asyncpg.Connection, row: asyncpg.Record, alert: asyncpg.Record | None, person: Person
) -> None:
    sms, email = _notification_text(row, alert)
    attempts = [("sms", person.phone, sms)] if person.phone else []
    attempts.append(("email", person.email, email))
    status, channel, error = "dead_lettered", attempts[-1][0], "no channel"
    for channel, address, text in attempts:
        msg = Message(channel, address, text, subject=f"Approval needed: {row['action']}")
        try:
            result = await asyncio.wait_for(
                get_adapter(channel).send(msg, f"appr:{row['id']}:{person.id}:{channel}"), SEND_TIMEOUT_S
            )
            status, error = result.status, ""
            break
        except (ChannelError, TimeoutError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.warning("approval %s: %s to %s failed: %s", row["id"], channel, person.id, error)
    if alert is None:
        return
    await conn.execute(
        "INSERT INTO deliveries (id, alert_id, recipient_user_id, recipient_address, channel, kind, "
        "notification_seq, idempotency_key, status, attempts, last_error, sent_at, delivered_at, priority) "
        "VALUES ($1, $2, $3, $4, $5, 'hard', 0, $6, $7, 1, $8, now(), now(), 9) ON CONFLICT DO NOTHING",
        uuid7(),
        alert["id"],
        person.id,
        address,
        channel,
        f"appr:{row['id']}:{person.id}:{channel}",
        status,
        error or None,
    )


async def request_approval(
    conn: asyncpg.Connection,
    *,
    origin: str,
    action: str,
    parameters: dict[str, Any],
    reason: str | None,
    risk: str | None,
    alert_id: UUID | None,
    requested_by: dict[str, Any],
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Create a pending approval, notify the approvers (hard SMS/email with `Reply APPROVE <id>`) and push
    SSE `approval.requested`. Returns the wire `Approval`."""
    if origin not in ORIGINS:
        raise AppError(422, "validation_error", f"origin: must be one of {', '.join(ORIGINS)}")
    risk = risk if risk in RISKS else "medium"
    actor = stored_actor(requested_by)
    alert = await conn.fetchrow("SELECT * FROM alerts WHERE id = $1", alert_id) if alert_id else None
    if alert_id and alert is None:
        raise not_found("alert")
    approver_ids = await approvers_for(conn, alert, actor)
    expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds or TTL_SECONDS)
    async with conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO approvals (id, status, origin, requested_by, alert_id, action, parameters, risk, "
            "reason, approver_user_ids, expires_at) "
            "VALUES ($1, 'pending', $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING *",
            uuid7(),
            origin,
            actor,
            alert_id,
            action,
            parameters or {},
            risk,
            reason,
            approver_ids,
            expires,
        )
        if alert is not None:
            await add_timeline(
                conn,
                alert["id"],
                "approval",
                f"Approval requested for {action} ({risk} risk) by {actor['name']}",
                actor=actor,
                data={"approvalId": str(row["id"]), "action": action},
            )
        await audit(
            conn, actor, "approval.request", "approval", str(row["id"]), {"action": action, "risk": risk}
        )
    for person in await users_by_ids(conn, approver_ids):
        await _send(conn, row, alert, person)
    (wire,) = await approvals_wire(conn, [row])
    await _safe_sse("approval.requested", wire, [alert["team_id"]] if alert else [], approver_ids)
    return wire


async def _safe_sse(event: str, data: dict[str, Any], team_ids: list[UUID], user_ids: list[UUID]) -> None:
    try:
        await publish_sse(event, data, team_ids=team_ids, user_ids=user_ids)
    except Exception:
        log.exception("SSE publish of %s failed", event)


async def can_decide(conn: asyncpg.Connection, row: asyncpg.Record, user_id: UUID) -> bool:
    """Listed approvers, the Domain Root of the alert's domains, and masters."""
    if user_id in row["approver_user_ids"]:
        return True
    scope = await load_scope(conn, user_id)
    if scope.is_master:
        return True
    if row["alert_id"] is None:
        return False
    domains = {
        r["domain_id"]
        for r in await conn.fetch(
            "SELECT td.domain_id FROM alerts a JOIN team_domains td ON td.team_id = a.team_id "
            "WHERE a.id = $1",
            row["alert_id"],
        )
    } | {await conn.fetchval("SELECT domain_id FROM alerts WHERE id = $1", row["alert_id"])}
    return any(
        r.role == "domain_root" and r.scope_type == "domain" and r.scope_id in domains for r in scope.roles
    )


async def expire_due(conn: asyncpg.Connection) -> list[UUID]:
    """Pending approvals past `expires_at` become `expired`; their step runs are marked skipped."""
    rows = await conn.fetch(
        "UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND expires_at <= now() RETURNING *"
    )
    for row in rows:
        await _after_decision(conn, row, f"Approval for {row['action']} expired")
        await conn.execute(
            "INSERT INTO step_runs (id, alert_id, step_id, state, approval_id, output, run_by) "
            "SELECT $1, alert_id, step_id, 'skipped', approval_id, 'approval expired', run_by "
            "FROM step_runs WHERE approval_id = $2 ORDER BY at DESC, id DESC LIMIT 1",
            uuid7(),
            row["id"],
        )
    return [r["id"] for r in rows]


async def _after_decision(conn: asyncpg.Connection, row: asyncpg.Record, text: str) -> None:
    await _wake(row)
    if row["alert_id"]:
        await add_timeline(conn, row["alert_id"], "approval", text, data={"approvalId": str(row["id"])})
    (wire,) = await approvals_wire(conn, [row])
    team_ids = []
    if row["alert_id"]:
        team_ids = [await conn.fetchval("SELECT team_id FROM alerts WHERE id = $1", row["alert_id"])]
    await _safe_sse("approval.decided", wire, team_ids, list(row["approver_user_ids"]))


async def _wake(row: asyncpg.Record) -> None:
    """Wake an MCP long-poll waiting on `appr:wait:{id}`."""
    message = orjson.dumps(
        {"status": row["status"], "decidedBy": row["decided_by"], "comment": row["comment"]}
    ).decode()
    try:
        await get_redis().publish(rkey("appr", "wait", str(row["id"])), message)
    except Exception:
        log.exception("could not publish appr:wait for %s", row["id"])


@dataclass
class DecisionResult:
    approval: dict[str, Any]
    outbox_ids: list[int] = field(default_factory=list)


async def decide_with_effects(
    conn: asyncpg.Connection,
    approval_id: UUID | str,
    *,
    decision: Literal["approve", "reject"],
    actor: dict[str, Any],
    comment: str | None = None,
) -> DecisionResult:
    if decision not in ("approve", "reject"):
        raise AppError(422, "validation_error", "decision: must be approve or reject")
    who = stored_actor(actor)
    user_id = parse_uuid(who["id"] or "")
    if who["type"] != "user" or user_id is None:
        raise forbidden("only a signed-in person can decide an approval")
    expired = False
    async with conn.transaction():
        row = await get_row(conn, approval_id, lock=True)
        if row is None:
            raise not_found("approval")
        if row["status"] == "pending" and row["expires_at"] <= datetime.now(UTC):
            expired = True
        elif row["status"] != "pending":
            raise AppError(409, "conflict", f"approval is already {row['status']}")
        elif not await can_decide(conn, row, user_id):
            raise forbidden("you are not an approver of this request")
        else:
            status = "approved" if decision == "approve" else "rejected"
            row = await conn.fetchrow(
                "UPDATE approvals SET status = $2, decided_by = $3, comment = $4 WHERE id = $1 RETURNING *",
                row["id"],
                status,
                who,
                (comment or "").strip() or None,
            )
            await audit(
                conn, who, f"approval.{status}", "approval", str(row["id"]), {"action": row["action"]}
            )
    if expired:
        await expire_due(conn)
        raise AppError(409, "conflict", "approval has expired")
    verb = "approved" if row["status"] == "approved" else "rejected"
    await _after_decision(
        conn, row, f"{who['name']} {verb} {row['action']}" + (f": {comment}" if comment else "")
    )
    outbox_ids: list[int] = []
    if row["origin"] == "runbook_step":
        from app.runbooks.runner import finish_approval

        outbox_ids = await finish_approval(conn, row, who)
    (wire,) = await approvals_wire(conn, [row])
    return DecisionResult(wire, outbox_ids)


async def decide(
    conn: asyncpg.Connection,
    approval_id: UUID,
    *,
    decision: Literal["approve", "reject"],
    actor: dict[str, Any],
    comment: str | None,
) -> dict[str, Any]:
    """Plan 3.5 signature. Approving a runbook-step approval runs the action. Raises 403 for anyone but a
    listed approver / Domain Root / master, 409 if already decided or expired."""
    return (
        await decide_with_effects(conn, approval_id, decision=decision, actor=actor, comment=comment)
    ).approval


async def list_approvals(conn: asyncpg.Connection, scope: Scope, status: str | None) -> list[dict[str, Any]]:
    """Approvals the caller may see: listed approver, requester, team in scope (master: all)."""
    await expire_due(conn)
    where, args = ["($1::text IS NULL OR a.status = $1)"], [status]
    if not scope.is_master:
        where.append(
            "($2 = ANY(a.approver_user_ids) OR a.requested_by->>'id' = $3 OR a.requested_by->>'userId' = $3 "
            "OR (a.alert_id IS NOT NULL AND al.team_id = ANY($4)))"
        )
        args += [scope.user_id, str(scope.user_id), list(scope.team_ids)]
    rows = await conn.fetch(
        "SELECT a.* FROM approvals a LEFT JOIN alerts al ON al.id = a.alert_id "
        f"WHERE {' AND '.join(where)} ORDER BY a.created_at DESC LIMIT 200",
        *args,
    )
    return await approvals_wire(conn, rows)
