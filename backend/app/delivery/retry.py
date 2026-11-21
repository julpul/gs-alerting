"""Retry / dead-letter / fallback policy of the delivery worker (spec 9.4, 4.5)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.core.ids import uuid7
from app.core.outbox import add_outbox
from app.core.timeline import add_timeline

MAX_ATTEMPTS = max(1, int(os.environ.get("DELIVERY_MAX_ATTEMPTS", "3")))
RETRY_LABELS = ("5s", "30s", "120s")
FALLBACK = {"phone": "sms", "sms": "email"}
GATEWAY_NAME = {"phone": "Phone", "sms": "SMS", "email": "Email", "webhook": "Webhook", "ui": "UI"}
TERMINAL_STATUSES = ("sent", "delivered", "acked", "dead_lettered", "suppressed_digest", "failed")


def retry_label(attempts: int) -> str:
    """Retry queue for a delivery that has now failed `attempts` times (1 -> 5s, 2 -> 30s, 3+ -> 120s)."""
    return RETRY_LABELS[min(max(attempts, 1), len(RETRY_LABELS)) - 1]


def should_dead_letter(attempts: int) -> bool:
    return attempts >= MAX_ATTEMPTS


async def create_fallback(
    conn: asyncpg.Connection,
    delivery: asyncpg.Record,
    person: asyncpg.Record | None,
    *,
    reason: str,
) -> dict[str, Any] | None:
    """Create the next-channel delivery for a dead-lettered one (phone -> sms -> email) and a timeline
    entry. Returns the new delivery (with `outbox_id`), or None when there is nothing to fall back to.
    """
    if person is None:
        return None
    channel = delivery["channel"]
    target = FALLBACK.get(channel)
    address = None
    while target:
        address = {"sms": person["phone"], "email": person["email"]}.get(target)
        if address:
            break
        target = FALLBACK.get(target)
    if not target:
        return None

    new_id = uuid7()
    key = f"{delivery['alert_id']}:{delivery['notification_seq']}:{person['id']}:{target}"
    inserted = await conn.fetchval(
        "INSERT INTO deliveries (id, alert_id, recipient_user_id, recipient_address, channel, kind, "
        "notification_seq, idempotency_key, status, priority, event_received_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', $9, $10) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        new_id,
        delivery["alert_id"],
        person["id"],
        address,
        target,
        delivery["kind"],
        delivery["notification_seq"],
        key,
        delivery["priority"],
        delivery["event_received_at"],
    )
    outbox_id = None
    if inserted is not None:
        outbox_id = await add_outbox(
            conn, "deliveries", target, {"deliveryId": str(new_id)}, delivery["priority"]
        )
    text = f"{GATEWAY_NAME[channel]} gateway down, fell back to {target}"
    await add_timeline(
        conn,
        delivery["alert_id"],
        "notified",
        text,
        data={
            "failedChannel": channel,
            "fallbackChannel": target,
            "recipient": person["display_name"],
            "error": reason,
            "alreadySent": inserted is None,
        },
    )
    return {"id": new_id if inserted else None, "channel": target, "outbox_id": outbox_id}


def delivery_event(
    row: asyncpg.Record | dict[str, Any], status: str, attempts: int, name: str | None
) -> dict[str, Any]:
    """`delivery.status` SSE payload (contract schema `Delivery`)."""
    user_id: UUID | None = row["recipient_user_id"]
    return {
        "id": str(row["id"]),
        "alertId": str(row["alert_id"]),
        "recipient": (
            {"kind": "user", "id": str(user_id), "displayName": name or str(user_id)} if user_id else None
        ),
        "channel": row["channel"],
        "kind": row["kind"],
        "status": status,
        "attempts": attempts,
        "updatedAt": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }
