"""Delivery worker role: consumes `deliveries.{ui,email,sms,phone,webhook}` (spec 9.4).

Message body: `{"deliveryId": "<uuid>"}`. Per message: load the row (skip if already sent), render, send with
a 3 s timeout, record the result. Failures go to the retry queues (5 s / 30 s / 120 s by attempt); after
`MAX_ATTEMPTS` failures the delivery is dead-lettered and a fallback delivery is created.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from uuid import UUID

import asyncpg
import orjson
from aio_pika.abc import AbstractIncomingMessage
from prometheus_client import start_http_server

from app.core.db import create_pool
from app.core.metrics import DELIVERIES, DELIVERY_LATENCY
from app.core.outbox import publish_outbox_rows
from app.core.rabbit import CHANNELS, DLQ_EXCHANGE, RETRY_EXCHANGE, Rabbit, retry_headers
from app.core.security import decrypt_secret
from app.core.sse import publish_sse
from app.core.timeline import add_timeline
from app.delivery.channels._client import SEND_TIMEOUT_S, close_clients
from app.delivery.channels.base import AlertView, Recipient
from app.delivery.channels.registry import get_adapter
from app.delivery.retry import (
    GATEWAY_NAME,
    TERMINAL_STATUSES,
    create_fallback,
    delivery_event,
    retry_label,
    should_dead_letter,
)

log = logging.getLogger(__name__)

PREFETCH = 50
NACK_DELAY_S = 1.0


async def _runbook(conn: asyncpg.Connection, alert: asyncpg.Record) -> tuple[str | None, list[str]]:
    """Runbook name and step texts if the runbooks module exists and finds one; never raises."""
    try:
        from app.runbooks.lookup import runbook_for_alert
    except ImportError:
        return None, []
    try:
        found = await runbook_for_alert(conn, dict(alert))
    except Exception:
        log.warning("runbook lookup failed for alert %s", alert["id"], exc_info=True)
        return None, []
    if found is None:
        return None, []

    def pick(obj: Any, *names: str) -> Any:
        for n in names:
            value = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
            if value:
                return value
        return None

    steps = [str(pick(s, "title", "text", "description", "name") or "") for s in pick(found, "steps") or []]
    return pick(found, "name"), [s for s in steps if s]


def _alert_view(alert: asyncpg.Record, runbook: tuple[str | None, list[str]]) -> AlertView:
    return AlertView(
        id=alert["id"],
        short_id=alert["short_id"],
        title=alert["title"],
        body=alert["body"] or "",
        severity=alert["severity"],
        status=alert["status"],
        count=alert["count"],
        escalation_level=alert["escalation_level"],
        team_id=alert["team_id"],
        fields=dict(alert["fields"] or {}),
        runbook_name=runbook[0],
        runbook_steps=runbook[1],
    )


class DeliveryWorker:
    def __init__(self, pool: asyncpg.Pool, rabbit: Rabbit) -> None:
        self.pool = pool
        self.rabbit = rabbit

    async def start(self) -> None:
        for channel in CHANNELS:
            await self.rabbit.consume(f"deliveries.{channel}", self._on_message, prefetch=PREFETCH)

    async def _on_message(self, msg: AbstractIncomingMessage) -> None:
        try:
            delivery_id = UUID(orjson.loads(msg.body)["deliveryId"])
        except (ValueError, KeyError, TypeError, orjson.JSONDecodeError):
            log.error("dropping malformed delivery message: %r", msg.body[:200])
            await msg.ack()
            return
        try:
            await self.process(delivery_id)
        except Exception:
            log.exception("delivery %s crashed; requeueing", delivery_id)
            await asyncio.sleep(NACK_DELAY_S)
            await msg.nack(requeue=True)
            return
        await msg.ack()

    async def process(self, delivery_id: UUID) -> str:
        """Handle one delivery; returns `delivered|sent|retrying|dead_lettered|skipped|missing`."""
        row = await self.pool.fetchrow("SELECT * FROM deliveries WHERE id = $1", delivery_id)
        if row is None:
            log.warning("delivery %s not found", delivery_id)
            return "missing"
        if row["status"] in TERMINAL_STATUSES:
            return "skipped"
        alert = await self.pool.fetchrow("SELECT * FROM alerts WHERE id = $1", row["alert_id"])
        person = (
            await self.pool.fetchrow(
                "SELECT id, email, phone, display_name FROM users WHERE id = $1", row["recipient_user_id"]
            )
            if row["recipient_user_id"]
            else None
        )
        if alert is None:
            log.warning("alert %s of delivery %s not found", row["alert_id"], delivery_id)
            return "missing"

        channel = row["channel"]
        adapter = get_adapter(channel)
        runbook: tuple[str | None, list[str]] = (None, [])
        secret = None
        async with self.pool.acquire() as conn:
            if channel == "email":
                runbook = await _runbook(conn, alert)
            if channel == "webhook":
                enc = await conn.fetchval(
                    "SELECT secret_encrypted FROM webhook_subscriptions WHERE team_id = $1 AND url = $2",
                    alert["team_id"],
                    row["recipient_address"],
                )
                secret = decrypt_secret(enc) if enc else None
        recipient = Recipient(
            name=person["display_name"] if person else "team",
            address=row["recipient_address"],
            user_id=row["recipient_user_id"],
            secret=secret,
        )
        try:
            msg = adapter.render(_alert_view(alert, runbook), recipient, row["kind"])
            result = await asyncio.wait_for(adapter.send(msg, row["idempotency_key"]), SEND_TIMEOUT_S)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            return await self._failed(row, alert, person, error)
        return await self._succeeded(row, alert, person, result.status)

    async def _succeeded(
        self, row: asyncpg.Record, alert: asyncpg.Record, person: asyncpg.Record | None, status: str
    ) -> str:
        updated = await self.pool.fetchrow(
            "UPDATE deliveries SET status = $2, attempts = attempts + 1, sent_at = now(), "
            "delivered_at = now(), last_error = NULL WHERE id = $1 "
            "AND status NOT IN ('sent', 'delivered', 'acked', 'dead_lettered') "
            "RETURNING attempts, delivered_at",
            row["id"],
            status,
        )
        if updated is None:
            return "skipped"
        channel = row["channel"]
        DELIVERIES.labels(channel, status).inc()
        if row["event_received_at"] is not None:
            DELIVERY_LATENCY.labels(channel).observe(
                max((updated["delivered_at"] - row["event_received_at"]).total_seconds(), 0.0)
            )
        if channel != "ui":
            name = person["display_name"] if person else "team"
            async with self.pool.acquire() as conn:
                await add_timeline(
                    conn,
                    row["alert_id"],
                    "notified",
                    f"{GATEWAY_NAME[channel]} {status} to {name}",
                    data={"channel": channel, "deliveryId": str(row["id"]), "kind": row["kind"]},
                )
        await self._publish_status(row, alert, person, status, updated["attempts"])
        return status

    async def _failed(
        self, row: asyncpg.Record, alert: asyncpg.Record, person: asyncpg.Record | None, error: str
    ) -> str:
        channel = row["channel"]
        attempts = await self.pool.fetchval(
            "UPDATE deliveries SET attempts = attempts + 1, last_error = $2, status = 'retrying' "
            "WHERE id = $1 RETURNING attempts",
            row["id"],
            error[:500],
        )
        log.warning("delivery %s via %s failed (attempt %s): %s", row["id"], channel, attempts, error)
        if not should_dead_letter(attempts):
            await self.rabbit.publish(
                RETRY_EXCHANGE,
                retry_label(attempts),
                {"deliveryId": str(row["id"])},
                priority=row["priority"],
                headers=retry_headers(channel),
            )
            await self._publish_status(row, alert, person, "retrying", attempts)
            return "retrying"

        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE deliveries SET status = 'dead_lettered' WHERE id = $1", row["id"])
            fallback = await create_fallback(conn, row, person, reason=error)
            if fallback is None:
                await add_timeline(
                    conn,
                    row["alert_id"],
                    "notified",
                    f"{GATEWAY_NAME[channel]} delivery failed after {attempts} attempts",
                    data={"channel": channel, "deliveryId": str(row["id"]), "error": error},
                )
        await self.rabbit.publish(
            DLQ_EXCHANGE,
            "",
            {"deliveryId": str(row["id"]), "channel": channel, "error": error, "attempts": attempts},
            priority=row["priority"],
        )
        if fallback and fallback["outbox_id"]:
            await publish_outbox_rows(self.pool, self.rabbit, [fallback["outbox_id"]])
        DELIVERIES.labels(channel, "dead_lettered").inc()
        await self._publish_status(row, alert, person, "dead_lettered", attempts)
        return "dead_lettered"

    async def _publish_status(
        self,
        row: asyncpg.Record,
        alert: asyncpg.Record,
        person: asyncpg.Record | None,
        status: str,
        attempts: int,
    ) -> None:
        try:
            await publish_sse(
                "delivery.status",
                delivery_event(row, status, attempts, person["display_name"] if person else None),
                team_ids=[alert["team_id"]],
                user_ids=[row["recipient_user_id"]] if row["recipient_user_id"] else [],
            )
        except Exception:
            log.warning("could not publish delivery.status SSE", exc_info=True)


async def run(stop: asyncio.Event) -> None:
    try:
        start_http_server(int(os.environ.get("METRICS_PORT", "9101")))
    except OSError:
        log.info("metrics port already in use; another role in this process serves /metrics")
    pool = await create_pool()
    rabbit = Rabbit()
    await rabbit.connect()
    await rabbit.declare_topology()
    worker = DeliveryWorker(pool, rabbit)
    await worker.start()
    log.info("delivery worker consuming %s", ", ".join(f"deliveries.{c}" for c in CHANNELS))
    try:
        await stop.wait()
    finally:
        await rabbit.close()
        await close_clients()
        await pool.close()
