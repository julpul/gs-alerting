"""Auto-resolve (spec 8): a preset's "resolved" notification resolves the open alert it refers to.

The alert is found the way the firing event found it: same rule, same fingerprint (`build_alert` runs the
rule's dedup fields over the resolved event, which carries the same labels as the firing one). No new
alert is ever created. The resolution is recorded as the `system` actor; the note goes to the timeline only,
because "resolved by the source" carries no knowledge for the fixed-before index.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any
from uuid import UUID

import asyncpg

from app.core.audit import SYSTEM_ACTOR, audit
from app.core.ids import format_short_id
from app.core.outbox import publish_outbox_rows
from app.core.rabbit import Rabbit
from app.core.sse import publish_sse
from app.core.timeline import add_timeline
from app.pipeline.fields import EventView
from app.pipeline.present import Renderer, build_alert
from app.pipeline.ruleview import cfg
from app.repositories.alert_actions import plan_resolved_updates

log = logging.getLogger(__name__)

DEFAULT_NOTE = "Resolved by source"


async def _resolve_one(
    conn: asyncpg.Connection, alert_id: UUID, note: str
) -> tuple[asyncpg.Record, list[int]] | None:
    """Resolve `alert_id` if it is still unresolved; returns the row and the outbox ids to publish."""
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM alerts WHERE id = $1 FOR UPDATE", alert_id)
        if row is None or row["status"] == "resolved":
            return None
        await conn.execute(
            "UPDATE alerts SET status = 'resolved', resolved_at = now(), "
            "ack_deadline = NULL, next_renotify_at = NULL WHERE id = $1",
            alert_id,
        )
        await add_timeline(conn, alert_id, "resolved", note, actor=SYSTEM_ACTOR, data={"auto": True})
        await audit(conn, SYSTEM_ACTOR, "alert.resolve", "alert", str(alert_id), {"auto": True})
        return row, await plan_resolved_updates(conn, alert_id)


async def resolve_from_source(
    pool: asyncpg.Pool,
    rabbit: Rabbit,
    candidates: Iterable[Any],
    ev: EventView,
    envelope: dict[str, Any],
    *,
    domain_id: UUID,
    render: Renderer,
    note: str | None,
) -> int:
    """Resolve the open alerts of every matching rule that `ev` refers to; returns how many were resolved."""
    resolved = 0
    for candidate in candidates:
        rule = candidate.rule
        _, fingerprint = build_alert(rule, ev, envelope, render=render, domain_id=domain_id)
        async with pool.acquire() as conn:
            alert_id = await conn.fetchval(
                "SELECT id FROM alerts WHERE rule_id = $1 AND fingerprint = $2 AND status <> 'resolved'",
                cfg(rule, "id"),
                fingerprint,
            )
            done = await _resolve_one(conn, alert_id, note or DEFAULT_NOTE) if alert_id else None
        if done is None:
            continue
        row, outbox_ids = done
        resolved += 1
        await _announce(pool, rabbit, row, outbox_ids)
    return resolved


async def _announce(pool: asyncpg.Pool, rabbit: Rabbit, row: asyncpg.Record, outbox_ids: list[int]) -> None:
    """After commit: soft "resolved" notifications and the `alert.resolved` SSE event (best effort)."""
    try:
        await publish_outbox_rows(pool, rabbit, outbox_ids)
        await publish_sse(
            "alert.resolved",
            {
                "alertId": str(row["id"]),
                "shortId": format_short_id(row["short_id"]),
                "by": {"kind": "system", "id": None, "displayName": "system"},
                "status": "resolved",
            },
            team_ids=[row["team_id"]],
        )
    except Exception:
        log.warning("announcing auto-resolve of alert %s failed", row["id"], exc_info=True)
