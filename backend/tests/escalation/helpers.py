"""Helpers for the escalation tests: rules, alerts, fake rabbit, SSE collector, polling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson

from app.core.ids import stable_id, uuid7
from app.core.redis import get_redis
from app.core.sse import team_channel
from tests.delivery.helpers import team, user, wait_for

__all__ = ["FakeRabbit", "SseCollector", "make_alert", "make_rule", "team", "user", "wait_for"]

SETTLEMENTS_DOMAIN = stable_id("domain", "settlements")


class FakeRabbit:
    """Records what would have been published, so job tests never feed real consumers."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict]] = []

    async def publish(self, exchange: str, routing_key: str, body: dict, *, priority: int = 0, headers=None):
        self.published.append((exchange, routing_key, body))


class SseCollector:
    """Subscribes to the Redis channel of a team and collects the events published on it."""

    def __init__(self, team_id: uuid.UUID) -> None:
        self._channel = team_channel(team_id)
        self._pubsub = get_redis().pubsub()
        self.events: list[dict[str, Any]] = []

    async def __aenter__(self) -> SseCollector:
        await self._pubsub.subscribe(self._channel)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._pubsub.unsubscribe(self._channel)
        await self._pubsub.aclose()

    async def of_type(self, event_type: str, *, wait_s: float = 5.0) -> list[dict[str, Any]]:
        async def check() -> list[dict[str, Any]]:
            while msg := await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05):
                self.events.append(orjson.loads(msg["data"]))
            return [e for e in self.events if e["type"] == event_type]

        return await wait_for(check, wait_s=wait_s, what=f"SSE {event_type}")


async def make_rule(
    pool,
    *,
    team_slug: str = "team-settlements",
    hard: list[dict] | None = None,
    soft: list[dict] | None = None,
    max_per_minute: int = 1000,
    escalation: dict | None = None,
) -> uuid.UUID:
    rule_id = uuid7()
    definition: dict[str, Any] = {
        "name": f"esc-{rule_id.hex[:8]}",
        "teamId": str(team(team_slug)),
        "match": {"op": "and", "conditions": []},
        "routing": {
            "hard": hard if hard is not None else [{"kind": "oncall", "id": team_slug}],
            "soft": soft or [],
        },
        "throttle": {"maxPerRecipientPerMinute": max_per_minute},
    }
    if escalation:
        definition["escalation"] = escalation
    await pool.execute(
        "INSERT INTO rules (id, team_id, name, definition) VALUES ($1, $2, $3, $4)",
        rule_id,
        team(team_slug),
        definition["name"],
        definition,
    )
    return rule_id


async def make_alert(
    pool,
    rule_id: uuid.UUID | None,
    *,
    team_slug: str = "team-settlements",
    domain_slug: str = "settlements",
    severity: str = "critical",
    plan: bool = True,
    due: bool = False,
) -> dict[str, Any]:
    """Insert an alert; with `plan` run the created-notification planning (arms deadline and renotify).

    `due` moves both timers into the past so the scheduler jobs pick the alert up.
    """
    from app.delivery.routing import plan_deliveries
    from app.repositories.alert_actions import _load_rule

    row = await pool.fetchrow(
        "INSERT INTO alerts (id, team_id, domain_id, rule_id, fingerprint, title, body, severity, "
        "first_event_received_at) VALUES ($1, $2, $3, $4, $5, $6, 'body', $7, $8) RETURNING *",
        uuid7(),
        team(team_slug),
        stable_id("domain", domain_slug),
        rule_id,
        uuid.uuid4().hex,
        f"Escalation test {uuid.uuid4().hex[:8]}",
        severity,
        datetime.now(UTC),
    )
    alert = dict(row)
    if plan:
        async with pool.acquire() as conn, conn.transaction():
            await plan_deliveries(conn, alert, await _load_rule(conn, alert), reason="created")
    if due:
        await make_due(pool, alert["id"])
    return dict(await pool.fetchrow("SELECT * FROM alerts WHERE id = $1", alert["id"]))


async def make_due(pool, alert_id: uuid.UUID) -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    await pool.execute(
        "UPDATE alerts SET ack_deadline = CASE WHEN ack_deadline IS NULL THEN NULL ELSE $2::timestamptz END, "
        "next_renotify_at = CASE WHEN next_renotify_at IS NULL THEN NULL ELSE $2::timestamptz END "
        "WHERE id = $1",
        alert_id,
        past,
    )


async def alert_row(pool, alert_id: uuid.UUID) -> dict[str, Any]:
    return dict(await pool.fetchrow("SELECT * FROM alerts WHERE id = $1", alert_id))


async def settle(pool) -> None:
    """Leave no pending outbox rows or armed alert timers behind: jobs act on every alert in the database."""
    await pool.execute("UPDATE outbox SET published_at = now() WHERE published_at IS NULL")
    await pool.execute(
        "UPDATE alerts SET ack_deadline = NULL, next_renotify_at = NULL "
        "WHERE ack_deadline IS NOT NULL OR next_renotify_at IS NOT NULL"
    )
