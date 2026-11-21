"""Batch reads the Herald views need on top of our repositories (one query per kind, never per alert).

Callers pass ids that already went through a scope-filtered repository call (`repositories.alerts`), or a
`Scope` that the query filters by.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg

from app.core.scope import Scope
from app.herald import views
from app.herald.ids import actor_ref, actor_user_ids, people_refs, team_slugs

ALERT_COLUMNS = (
    "a.id, a.short_id, a.team_id, a.domain_id, a.rule_id, a.source_id, a.title, a.body, a.severity, a.color, "
    "a.status, a.count, a.first_seen_at, a.last_seen_at, a.escalation_level, a.ack_deadline, a.acked_at, "
    "a.acked_by, a.resolved_at, a.assignee_id, a.flags, a.fields, s.name AS source_name"
)
DELIVERY_COLUMNS = (
    "d.id, d.alert_id, d.recipient_user_id, d.recipient_address, d.channel, d.kind, d.status, d.attempts, "
    "d.last_error, d.created_at, "
    "extract(epoch FROM d.delivered_at - d.event_received_at) * 1000 AS latency_ms, "
    "(SELECT t.slug FROM alerts ta JOIN teams t ON t.id = ta.team_id WHERE ta.id = d.alert_id) AS team_slug, "
    "(SELECT t.name FROM alerts ta JOIN teams t ON t.id = ta.team_id WHERE ta.id = d.alert_id) AS team_name"
)


async def _deliveries(conn: asyncpg.Connection, alert_ids: list[UUID]) -> dict[UUID, list[asyncpg.Record]]:
    rows = await conn.fetch(
        f"SELECT {DELIVERY_COLUMNS} FROM deliveries d WHERE d.alert_id = ANY($1) ORDER BY d.created_at, d.id",
        alert_ids,
    )
    out: dict[UUID, list[asyncpg.Record]] = defaultdict(list)
    for r in rows:
        out[r["alert_id"]].append(r)
    return out


async def _resolvers(conn: asyncpg.Connection, alert_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT DISTINCT ON (alert_id) alert_id, actor FROM timeline "
        "WHERE alert_id = ANY($1) AND type = 'resolved' ORDER BY alert_id, at DESC, id DESC",
        alert_ids,
    )
    return {r["alert_id"]: r["actor"] for r in rows}


async def herald_alerts(conn: asyncpg.Connection, alert_ids: list[UUID]) -> list[dict[str, Any]]:
    """Herald `Alert`s for ids already checked against the caller's scope, in the given order."""
    if not alert_ids:
        return []
    rows = await conn.fetch(
        f"SELECT {ALERT_COLUMNS} FROM alerts a LEFT JOIN sources s ON s.id = a.source_id "
        "WHERE a.id = ANY($1)",
        alert_ids,
    )
    by_id = {r["id"]: r for r in rows}
    deliveries = await _deliveries(conn, alert_ids)
    resolvers = await _resolvers(conn, alert_ids)
    slugs = await team_slugs(conn)
    users = {r["acked_by"] for r in rows} | {r["assignee_id"] for r in rows}
    users |= {d["recipient_user_id"] for ds in deliveries.values() for d in ds}
    users |= actor_user_ids(resolvers.values())
    refs = await people_refs(conn, users)
    out = []
    for alert_id in alert_ids:
        row = by_id.get(alert_id)
        if row is None:
            continue
        resolver = resolvers.get(alert_id)
        out.append(
            views.alert_out(
                row,
                slug=slugs.get(row["team_id"], str(row["team_id"])),
                refs=refs,
                deliveries=[
                    views.delivery_out(d, refs, short_id=row["short_id"])
                    for d in deliveries.get(alert_id, [])
                ],
                resolved_by=actor_ref(resolver, refs) if resolver else None,
            )
        )
    return out


async def recent_deliveries(
    conn: asyncpg.Connection, scope: Scope, team_id: UUID | None, limit: int
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {DELIVERY_COLUMNS}, a.short_id, a.title "
        "FROM deliveries d JOIN alerts a ON a.id = d.alert_id "
        "WHERE ($1::bool OR a.team_id = ANY($2)) AND ($3::uuid IS NULL OR a.team_id = $3) "
        "ORDER BY d.created_at DESC, d.id DESC LIMIT $4",
        scope.is_master,
        list(scope.team_ids),
        team_id,
        limit,
    )
    refs = await people_refs(conn, {r["recipient_user_id"] for r in rows})
    return [views.delivery_out(r, refs, short_id=r["short_id"]) for r in rows]


async def audit_entries(conn: asyncpg.Connection, scope: Scope, limit: int) -> list[dict[str, Any]]:
    """Newest `limit` audit rows the caller may see, oldest first (Herald order).

    Master: everything. Others: entries about alerts / approvals / rules / sources of their teams, and
    their own actions."""
    rows = await conn.fetch(
        "SELECT l.id, l.at, l.actor, l.action, l.target_type, l.target_id, l.data FROM audit_log l "
        "WHERE $1::bool OR l.actor->>'id' = $2 OR l.actor->>'userId' = $2 "
        "OR (l.target_type = 'alert' AND EXISTS (SELECT 1 FROM alerts a WHERE a.id::text = l.target_id "
        "    AND a.team_id = ANY($3))) "
        "OR (l.target_type = 'approval' AND EXISTS (SELECT 1 FROM approvals p "
        "    JOIN alerts a ON a.id = p.alert_id "
        "    WHERE p.id::text = l.target_id AND a.team_id = ANY($3))) "
        "OR (l.target_type = 'rule' AND EXISTS (SELECT 1 FROM rules r WHERE r.id::text = l.target_id "
        "    AND r.team_id = ANY($3))) "
        "OR (l.target_type = 'source' AND EXISTS (SELECT 1 FROM sources s WHERE s.id::text = l.target_id "
        "    AND s.team_id = ANY($3))) "
        "ORDER BY l.id DESC LIMIT $4",
        scope.is_master,
        str(scope.user_id),
        list(scope.team_ids),
        limit,
    )
    rows = list(reversed(rows))
    alert_ids = [r["target_id"] for r in rows if r["target_type"] == "alert"]
    shorts = {
        str(r["id"]): r["short_id"]
        for r in await conn.fetch("SELECT id, short_id FROM alerts WHERE id::text = ANY($1)", alert_ids)
    }
    refs = await people_refs(conn, actor_user_ids(r["actor"] for r in rows))
    return [views.audit_out(r, refs, shorts) for r in rows]


OUTSTANDING_WINDOW = timedelta(hours=1)


async def delivery_stats(
    conn: asyncpg.Connection, scope: Scope, since: float, run: str | None
) -> dict[str, Any]:
    """Herald `/api/stats` numbers over our deliveries/alerts (event received -> delivered latency).

    `outstanding` ("Waiting to be sent") is what is queued right now: deliveries still `pending` or
    `retrying` that were created in the last hour (older ones are stuck, not waiting; they show up as
    retrying / dead-lettered in the delivery log)."""
    row = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE d.delivered_at IS NOT NULL OR d.status IN ('delivered', 'acked')) "
        "         AS delivered, "
        "       count(*) FILTER (WHERE d.delivered_at IS NULL AND d.status IN ('pending', 'retrying') "
        "         AND d.created_at >= now() - $5::interval) AS outstanding, "
        "       percentile_cont(0.5) WITHIN GROUP (ORDER BY x.ms) AS p50_ms, "
        "       percentile_cont(0.99) WITHIN GROUP (ORDER BY x.ms) AS p99_ms, "
        "       max(x.ms) AS max_ms, extract(epoch FROM max(d.delivered_at)) AS last_delivered "
        "FROM deliveries d JOIN alerts a ON a.id = d.alert_id "
        "CROSS JOIN LATERAL (SELECT extract(epoch FROM d.delivered_at - d.event_received_at) * 1000 AS ms) x "
        "WHERE d.created_at >= to_timestamp($1) AND ($2::bool OR a.team_id = ANY($3)) "
        "AND ($4::text IS NULL OR a.raw_event->>'run' = $4)",
        since,
        scope.is_master,
        list(scope.team_ids),
        run,
        OUTSTANDING_WINDOW,
    )
    events = await conn.fetchval(
        "SELECT coalesce(sum(count), 0) FROM alerts a WHERE a.first_seen_at >= to_timestamp($1) "
        "AND ($2::bool OR a.team_id = ANY($3)) AND ($4::text IS NULL OR a.raw_event->>'run' = $4)",
        since,
        scope.is_master,
        list(scope.team_ids),
        run,
    )
    out = {k: (float(v) if v is not None else None) for k, v in dict(row).items()}
    return {**out, "matched_events": int(events)}
