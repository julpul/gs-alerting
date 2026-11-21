"""`escalate`: internal. Pages the on-call of another team (default: the alert's own team) about this alert.

The target team is a slug or uuid. If it does not exist the alert's domain roots are paged instead, so an
escalation never silently goes nowhere. One hard delivery per person (SMS, or email without a phone).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel

from app.core.ids import parse_uuid, uuid7
from app.core.outbox import add_outbox
from app.core.timeline import add_timeline
from app.delivery.recipients import Person, domain_roots, users_by_ids
from app.escalation.oncall import current_oncall
from app.runbooks.actions.registry import ActionContext, ActionResult, register

SEVERITY_PRIORITY = {"critical": 9, "error": 6, "warning": 3, "info": 0}


class Params(BaseModel):
    team: str | None = None
    reason: str | None = None


async def _team(conn: asyncpg.Connection, ref: str) -> asyncpg.Record | None:
    uid = parse_uuid(ref)
    if uid is not None:
        return await conn.fetchrow("SELECT id, name FROM teams WHERE id = $1", uid)
    return await conn.fetchrow("SELECT id, name FROM teams WHERE slug = $1", ref)


async def _insert(
    conn: asyncpg.Connection, ctx: ActionContext, person: Person, seq: int, priority: int
) -> bool:
    channel, address = ("sms", person.phone) if person.phone else ("email", person.email)
    alert_id: UUID = ctx.alert["id"]
    key = f"{alert_id}:esc:{ctx.run_id}:{person.id}:{channel}"
    delivery_id = uuid7()
    inserted = await conn.fetchval(
        "INSERT INTO deliveries (id, alert_id, recipient_user_id, recipient_address, channel, kind, "
        "notification_seq, idempotency_key, status, priority, event_received_at) "
        "VALUES ($1, $2, $3, $4, $5, 'hard', $6, $7, 'pending', $8, $9) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        delivery_id,
        alert_id,
        person.id,
        address,
        channel,
        seq,
        key,
        priority,
        ctx.alert.get("first_event_received_at") or ctx.alert.get("first_seen_at"),
    )
    if inserted is None:
        return False
    ctx.outbox_ids.append(
        await add_outbox(conn, "deliveries", channel, {"deliveryId": str(delivery_id)}, priority)
    )
    return True


@register
class Escalate:
    name = "escalate"
    params_schema = Params
    default_risk = "low"
    label = "Escalate to {team}"
    affects_trust = True

    async def run(self, params: Params, ctx: ActionContext) -> ActionResult:
        conn = ctx.conn
        if conn is None:
            return ActionResult(False, "escalate needs a database connection")
        alert: dict[str, Any] = ctx.alert
        target = await _team(conn, params.team) if params.team else None
        if params.team and target is None:
            people = await domain_roots(conn, alert["domain_id"])
            where = f"domain roots (team {params.team!r} does not exist)"
        else:
            team_id = target["id"] if target else alert["team_id"]
            people = await users_by_ids(conn, await current_oncall(conn, team_id))
            where = f"on-call of {target['name'] if target else 'the alert team'}"
        if not people:
            return ActionResult(False, f"nobody to escalate to: no {where}")
        seq = await conn.fetchval(
            "SELECT COALESCE(MAX(notification_seq), 0) + 1 FROM deliveries WHERE alert_id = $1", alert["id"]
        )
        priority = SEVERITY_PRIORITY.get(alert.get("severity", ""), 0)
        sent = [p for p in people if await _insert(conn, ctx, p, seq, priority)]
        names = ", ".join(p.name for p in sent)
        await add_timeline(
            conn,
            alert["id"],
            "escalated",
            f"Escalated to {where}: {names}",
            actor=ctx.actor,
            data={"stepId": ctx.step_id, "reason": params.reason},
        )
        return ActionResult(True, f"paged {len(sent)} person(s) at {where}: {names}", {"paged": len(sent)})
