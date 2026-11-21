"""Alert upsert (spec 4.4 step 5, spec 8): dedup, grouping, notify-on-state-change, all in ONE transaction.

New alert  -> timeline `created`, automatic incident manager (`escalation.auto_assign`), deliveries
              (`plan_deliveries`), outbox rows `ai.tasks` + SSE `alert.created`.
Duplicate  -> `count++`, `last_seen_at`, grouped item appended (cap 100); nothing else unless the severity
              went up (then deliveries `severity_up` + SSE `alert.updated`).
The caller publishes `UpsertResult.outbox_ids` after commit (`core.outbox.publish_outbox_rows`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.core.ids import uuid7
from app.core.outbox import add_outbox
from app.core.timeline import add_timeline
from app.escalation.auto_assign import assign_new_alert
from app.pipeline.envelope import SEVERITY_RANK, format_ts, parse_received_at, priority_for
from app.pipeline.fields import EventView
from app.pipeline.fingerprint import knowledge_key
from app.pipeline.injection import FLAG as SUSPICIOUS
from app.pipeline.ruleview import cfg

GROUPED_ITEMS_CAP = 100

Planner = Callable[..., Awaitable[list[dict[str, Any]]]]

_UPSERT = f"""
WITH prev AS (
    SELECT severity FROM alerts WHERE rule_id = $4 AND fingerprint = $6 AND status <> 'resolved'
)
INSERT INTO alerts (id, team_id, domain_id, rule_id, source_id, fingerprint, knowledge_key, title, body,
                    severity, color, first_event_received_at, flags, fields, raw_event, grouped_items,
                    runbook_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
ON CONFLICT (rule_id, fingerprint) WHERE status <> 'resolved' DO UPDATE SET
    count = alerts.count + 1,
    last_seen_at = now(),
    severity = CASE
        WHEN array_position(ARRAY['info', 'warning', 'error', 'critical'], EXCLUDED.severity)
           > array_position(ARRAY['info', 'warning', 'error', 'critical'], alerts.severity)
        THEN EXCLUDED.severity ELSE alerts.severity END,
    flags = ARRAY(SELECT DISTINCT f FROM unnest(alerts.flags || EXCLUDED.flags) AS f ORDER BY f),
    grouped_items = CASE
        WHEN jsonb_array_length(EXCLUDED.grouped_items) = 0
          OR jsonb_array_length(alerts.grouped_items) >= {GROUPED_ITEMS_CAP} THEN alerts.grouped_items
        ELSE alerts.grouped_items || EXCLUDED.grouped_items END
RETURNING *, (xmax = 0) AS inserted, (SELECT severity FROM prev) AS prev_severity
"""

# Fast path for duplicates: a plain UPDATE of the open alert. Besides being cheaper than the upsert, it
# avoids consuming `short_id` sequence values (the upsert evaluates column defaults before detecting
# the conflict, which made A-6 jump to A-507 after a 500-event storm).
_BUMP = (
    """
WITH prev AS (
    SELECT id, severity FROM alerts
    WHERE rule_id = $1 AND fingerprint = $2 AND status <> 'resolved'
    FOR UPDATE
)
UPDATE alerts AS a SET
    count = a.count + 1,
    last_seen_at = now(),
    severity = CASE
        WHEN array_position(ARRAY['info', 'warning', 'error', 'critical'], $3::text)
           > array_position(ARRAY['info', 'warning', 'error', 'critical'], a.severity)
        THEN $3::text ELSE a.severity END,
    flags = ARRAY(SELECT DISTINCT f FROM unnest(a.flags || $4::text[]) AS f ORDER BY f),
    grouped_items = CASE
        WHEN jsonb_array_length($5::jsonb) = 0
          OR jsonb_array_length(a.grouped_items) >= """
    + str(GROUPED_ITEMS_CAP)
    + """ THEN a.grouped_items
        ELSE a.grouped_items || $5::jsonb END
FROM prev
WHERE a.id = prev.id
RETURNING a.*, false AS inserted, prev.severity AS prev_severity
"""
)


@dataclass
class RenderedAlert:
    """Everything the writer needs that came from templates / rule config / normalization."""

    title: str
    body: str | None
    severity: str
    color: str | None
    fields: list[dict[str, str]]
    flags: list[str]
    domain_id: UUID
    grouped_item: dict[str, Any] | None = None


@dataclass
class UpsertResult:
    alert_id: UUID
    inserted: bool
    count: int
    outbox_ids: list[int] = field(default_factory=list)
    alert: dict[str, Any] = field(default_factory=dict)
    severity_increased: bool = False


def alert_summary(alert: dict[str, Any]) -> dict[str, Any]:
    """The `AlertSummary` wire shape (camelCase) carried by SSE `alert.created|updated`."""

    def ts(value: datetime | None) -> str | None:
        return format_ts(value) if value else None

    return {
        "id": str(alert["id"]),
        "title": alert["title"],
        "severity": alert["severity"],
        "color": alert["color"],
        "status": alert["status"],
        "count": alert["count"],
        "firstSeenAt": ts(alert["first_seen_at"]),
        "lastSeenAt": ts(alert["last_seen_at"]),
        "teamId": str(alert["team_id"]),
        "domainId": str(alert["domain_id"]),
        "ruleId": str(alert["rule_id"]) if alert["rule_id"] else None,
        "escalationLevel": alert["escalation_level"],
        "ackDeadline": ts(alert["ack_deadline"]),
        "flags": list(alert["flags"]),
    }


def sse_payload(event_type: str, alert: dict[str, Any]) -> dict[str, Any]:
    """Same `{type, data, at}` message shape that `core.sse.publish_sse` emits."""
    return {"type": event_type, "data": alert_summary(alert), "at": format_ts(datetime.now(UTC))}


def team_routing_key(team_id: UUID | str) -> str:
    return f"sse:team:{team_id}"


def _default_planner() -> Planner:
    from app.delivery.routing import plan_deliveries

    return plan_deliveries


async def _outbox_ids_of_this_tx(conn: asyncpg.Connection) -> list[int]:
    """Outbox rows created in the current transaction (their `created_at` is its `now()`)."""
    rows = await conn.fetch(
        "SELECT id FROM outbox WHERE created_at = now() AND published_at IS NULL ORDER BY id"
    )
    return [r["id"] for r in rows]


async def upsert_alert(
    conn: asyncpg.Connection,
    *,
    rule: Any,
    ev: EventView,
    envelope: dict[str, Any],
    rendered: RenderedAlert,
    fingerprint: str,
    planner: Planner | None = None,
) -> UpsertResult:
    """`rule` is the `CompiledRule` (`.rule` = the `RuleDef`). Runs in one transaction (own or caller's)."""
    rule_def = getattr(rule, "rule", rule)
    team_id = cfg(rule_def, "team_id")
    team_uuid = team_id if isinstance(team_id, UUID) else UUID(str(team_id))
    rule_id = cfg(rule_def, "id")
    rule_uuid = rule_id if isinstance(rule_id, UUID) else UUID(str(rule_id))
    runbook_id = cfg(rule_def, "runbook_id")
    meta = ev.meta
    key = knowledge_key(
        rendered.domain_id,
        meta.get("type") or meta.get("schema"),
        meta.get("service"),
        meta.get("error_code"),
    )
    grouped = [rendered.grouped_item] if rendered.grouped_item is not None else []
    flags = sorted(set(rendered.flags))

    async with conn.transaction():
        row = await conn.fetchrow(_BUMP, rule_uuid, fingerprint, rendered.severity, flags, grouped)
        if row is None:  # no open alert yet (or a race with another worker: the upsert handles it)
            row = await _insert(
                conn, envelope, rendered, team_uuid, rule_uuid, fingerprint, key, flags, grouped, runbook_id
            )
        alert = dict(row)
        inserted = alert.pop("inserted")
        prev_severity = alert.pop("prev_severity")
        increased = (
            not inserted
            and prev_severity is not None
            and SEVERITY_RANK[alert["severity"]] > SEVERITY_RANK[prev_severity]
        )
        if inserted or increased:
            plan = planner or _default_planner()
            await _on_state_change(conn, alert, rule_def, plan, inserted=inserted)
        outbox_ids = await _outbox_ids_of_this_tx(conn) if inserted or increased else []
    return UpsertResult(
        alert_id=alert["id"],
        inserted=inserted,
        count=alert["count"],
        outbox_ids=outbox_ids,
        alert=alert,
        severity_increased=increased,
    )


async def _insert(
    conn: asyncpg.Connection,
    envelope: dict[str, Any],
    rendered: RenderedAlert,
    team_uuid: UUID,
    rule_uuid: UUID,
    fingerprint: str,
    key: str,
    flags: list[str],
    grouped: list[dict[str, Any]],
    runbook_id: Any,
) -> asyncpg.Record:
    return await conn.fetchrow(
        _UPSERT,
        uuid7(),
        team_uuid,
        rendered.domain_id,
        rule_uuid,
        UUID(envelope["source_id"]),
        fingerprint,
        key,
        rendered.title,
        rendered.body,
        rendered.severity,
        rendered.color,
        parse_received_at(envelope["received_at"]),
        flags,
        rendered.fields,
        envelope["raw"],
        grouped,
        UUID(str(runbook_id)) if runbook_id else None,
    )


async def _on_state_change(
    conn: asyncpg.Connection, alert: dict[str, Any], rule_def: Any, plan: Planner, *, inserted: bool
) -> None:
    alert_id = alert["id"]
    if inserted:
        await add_timeline(
            conn,
            alert_id,
            "created",
            f'Alert created by rule "{cfg(rule_def, "name", "")}"',
            data={"ruleId": str(alert["rule_id"]), "fingerprint": alert["fingerprint"]},
        )
        if SUSPICIOUS in alert["flags"]:
            await add_timeline(
                conn,
                alert_id,
                "created",
                "Suspicious content detected in the event; AI action suggestions are disabled",
                data={"flag": SUSPICIOUS},
            )
        decision = await assign_new_alert(conn, alert, rule_def)
        if decision is not None:
            alert["assignee_id"] = decision.user_id
        await plan(conn, alert, rule_def, reason="created")
        await add_outbox(
            conn,
            "ai",
            "ai.tasks",
            {
                "kind": "suggest_fix",
                "alert_id": str(alert_id),
                "team_id": str(alert["team_id"]),
                "domain_id": str(alert["domain_id"]),
                "knowledge_key": alert["knowledge_key"],
                "flags": list(alert["flags"]),
            },
            priority=priority_for(alert["severity"]),
        )
        sse_type = "alert.created"
    else:
        await add_timeline(
            conn,
            alert_id,
            "deduplicated",
            f"Severity increased to {alert['severity']}",
            data={"severity": alert["severity"]},
        )
        await plan(conn, alert, rule_def, reason="severity_up")
        sse_type = "alert.updated"
    await add_outbox(conn, "sse", team_routing_key(alert["team_id"]), sse_payload(sse_type, alert))
