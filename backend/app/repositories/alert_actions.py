"""Alert mutations (ack, resolve, assign, comment). Run inside the caller's transaction.

Callers publish the SSE events and outbox rows after commit.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg

from app.core.audit import audit, user_actor
from app.core.errors import AppError, forbidden
from app.core.ids import uuid7
from app.core.scope import Scope, load_scope
from app.core.timeline import add_timeline
from app.repositories.alerts import timeline_out
from app.schemas.internal import User

log = logging.getLogger(__name__)

ACKABLE = ("open", "escalated")


async def require_actor_role(conn: asyncpg.Connection, scope: Scope, alert: asyncpg.Record) -> None:
    """ack / resolve / assign need master, domain_root of one of the team's domains, or team member."""
    if scope.is_master:
        return
    rows = await conn.fetch("SELECT domain_id FROM team_domains WHERE team_id = $1", alert["team_id"])
    domains = {r["domain_id"] for r in rows} | {alert["domain_id"]}
    for r in scope.roles:
        if r.role == "member" and r.scope_type == "team" and r.scope_id == alert["team_id"]:
            return
        if r.role == "domain_root" and r.scope_type == "domain" and r.scope_id in domains:
            return
    raise forbidden("you need the member role (or higher) on this team to change alerts")


def _by(actor: dict[str, Any], user: User) -> str:
    """ "Anna" for a person, "agent X on behalf of Anna" for an agent acting with the user's permissions."""
    if actor.get("type") == "agent":
        return f"agent {actor.get('name')} on behalf of {user.display_name}"
    return user.display_name


async def ack_alert(
    conn: asyncpg.Connection, alert: asyncpg.Record, user: User, actor: dict[str, Any] | None = None
) -> bool:
    """Returns True if the state changed (False when it was already acknowledged).

    `actor` defaults to the user; agents pass their own actor (spec 13.1.3) to audit "agent X on behalf of Y".
    """
    if alert["status"] == "acknowledged":
        return False
    if alert["status"] not in ACKABLE:
        raise AppError(409, "conflict", f"alert is {alert['status']} and cannot be acknowledged")
    actor = actor or user_actor(user.id, user.display_name)
    await conn.execute(
        "UPDATE alerts SET status = 'acknowledged', acked_at = now(), acked_by = $2, "
        "ack_deadline = NULL, next_renotify_at = NULL WHERE id = $1",
        alert["id"],
        user.id,
    )
    await conn.execute(
        "UPDATE deliveries SET status = 'acked' "
        "WHERE alert_id = $1 AND kind = 'hard' AND status IN ('sent', 'delivered')",
        alert["id"],
    )
    await add_timeline(conn, alert["id"], "acked", f"Acknowledged by {_by(actor, user)}", actor=actor)
    await audit(conn, actor, "alert.ack", "alert", str(alert["id"]))
    return True


async def resolve_alert(
    conn: asyncpg.Connection, alert: asyncpg.Record, user: User, note: str | None
) -> bool:
    """Returns True if the state changed (False when it was already resolved)."""
    if alert["status"] == "resolved":
        return False
    actor = user_actor(user.id, user.display_name)
    note = (note or "").strip() or None
    await conn.execute(
        "UPDATE alerts SET status = 'resolved', resolved_at = now(), "
        "ack_deadline = NULL, next_renotify_at = NULL WHERE id = $1",
        alert["id"],
    )
    knowledge_key = await conn.fetchval("SELECT knowledge_key FROM alerts WHERE id = $1", alert["id"])
    if note:
        await conn.execute(
            "INSERT INTO resolutions (id, alert_id, domain_id, team_id, knowledge_key, note, resolved_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid7(),
            alert["id"],
            alert["domain_id"],
            alert["team_id"],
            knowledge_key,
            note,
            user.id,
        )
    await add_timeline(
        conn,
        alert["id"],
        "resolved",
        f"Resolved by {user.display_name}",
        actor=actor,
        data={"resolutionNote": note} if note else None,
    )
    await audit(conn, actor, "alert.resolve", "alert", str(alert["id"]), {"hasNote": note is not None})
    return True


async def plan_resolved_updates(conn: asyncpg.Connection, alert_id: UUID) -> list[int]:
    """Soft "resolved" notifications through the routing module when it exists.

    Returns ids of outbox rows created (to publish after commit). Failures never block the resolve.
    """
    try:
        from app.delivery.routing import plan_deliveries
    except ImportError:
        return []
    started = await conn.fetchval("SELECT now()")
    try:
        async with conn.transaction():
            alert = await conn.fetchrow("SELECT * FROM alerts WHERE id = $1", alert_id)
            await plan_deliveries(conn, dict(alert), await _load_rule(conn, alert), reason="resolved")
    except Exception:
        log.exception("could not plan resolved notifications for alert %s", alert_id)
        return []
    rows = await conn.fetch(
        "SELECT id FROM outbox WHERE created_at = $1 AND published_at IS NULL AND topic <> 'sse'", started
    )
    return [r["id"] for r in rows]


async def _load_rule(conn: asyncpg.Connection, alert: asyncpg.Record) -> Any:
    if alert["rule_id"] is None:
        return None
    try:
        from app.pipeline.rules.model import RuleDef
    except ImportError:
        return None
    row = await conn.fetchrow("SELECT team_id, definition FROM rules WHERE id = $1", alert["rule_id"])
    if row is None:
        return None
    try:
        return RuleDef.model_validate(
            {**row["definition"], "id": alert["rule_id"], "team_id": row["team_id"]}
        )
    except Exception:
        log.warning("rule %s could not be loaded for resolved notifications", alert["rule_id"])
        return None


async def assign_alert(
    conn: asyncpg.Connection, alert: asyncpg.Record, user: User, assignee_id: UUID
) -> bool:
    if alert["assignee_id"] == assignee_id:
        return False
    target = await conn.fetchrow("SELECT id, display_name FROM users WHERE id = $1", assignee_id)
    if target is None:
        raise AppError(422, "validation_error", "userId: user not found")
    target_scope = await load_scope(conn, assignee_id)
    if not target_scope.can_see_team(alert["team_id"]):
        raise AppError(422, "validation_error", "userId: user does not belong to the alert's team")
    actor = user_actor(user.id, user.display_name)
    await conn.execute("UPDATE alerts SET assignee_id = $2 WHERE id = $1", alert["id"], assignee_id)
    await add_timeline(
        conn,
        alert["id"],
        "assigned",
        f"Assigned to {target['display_name']} by {user.display_name}",
        actor=actor,
        data={"assigneeId": str(assignee_id)},
    )
    await audit(conn, actor, "alert.assign", "alert", str(alert["id"]), {"assigneeId": str(assignee_id)})
    return True


async def unassign_alert(conn: asyncpg.Connection, alert: asyncpg.Record, user: User) -> bool:
    if alert["assignee_id"] is None:
        return False
    actor = user_actor(user.id, user.display_name)
    await conn.execute("UPDATE alerts SET assignee_id = NULL WHERE id = $1", alert["id"])
    await add_timeline(conn, alert["id"], "assigned", f"Unassigned by {user.display_name}", actor=actor)
    await audit(conn, actor, "alert.unassign", "alert", str(alert["id"]), {})
    return True


async def add_comment(
    conn: asyncpg.Connection,
    alert: asyncpg.Record,
    user: User,
    text: str,
    actor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actor = actor or user_actor(user.id, user.display_name)
    row = await conn.fetchrow(
        "INSERT INTO timeline (id, alert_id, at, type, actor, text, data) "
        "VALUES ($1, $2, now(), 'comment', $3, $4, '{}') RETURNING id, at, type, actor, text, data",
        uuid7(),
        alert["id"],
        actor,
        text,
    )
    await audit(conn, actor, "alert.comment", "alert", str(alert["id"]))
    return timeline_out(row)
