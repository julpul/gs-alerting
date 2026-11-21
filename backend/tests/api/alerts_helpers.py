"""SQL helpers that insert alerts/sources/rules/deliveries for the alerts API tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.core.ids import stable_id, uuid7

SOURCE_ID = stable_id("test-source", "alerts-api")
RULE_ID = stable_id("test-rule", "alerts-api")


def team(slug: str) -> UUID:
    return stable_id("team", slug)


def domain(slug: str) -> UUID:
    return stable_id("domain", slug)


def user(email: str) -> UUID:
    return stable_id("user", email)


async def ensure_source_and_rule(pool: asyncpg.Pool, team_slug: str = "team-payments") -> None:
    domain_slug = {"team-payments": "payments", "team-settlements": "settlements"}[team_slug]
    await pool.execute(
        "INSERT INTO sources (id, name, team_id, domain_id, type, key_prefix, key_hash) "
        "VALUES ($1, 'Test source', $2, $3, 'webhook', 'ak_test', 'hash-alerts-api') ON CONFLICT DO NOTHING",
        SOURCE_ID,
        team(team_slug),
        domain(domain_slug),
    )
    await pool.execute(
        "INSERT INTO rules (id, team_id, name, definition) VALUES ($1, $2, 'Test rule', '{}') "
        "ON CONFLICT DO NOTHING",
        RULE_ID,
        team(team_slug),
    )


async def make_alert(
    pool: asyncpg.Pool,
    *,
    team_slug: str = "team-payments",
    domain_slug: str = "payments",
    title: str = "Test alert",
    severity: str = "error",
    status: str = "open",
    last_seen_at: datetime | None = None,
    count: int = 1,
    flags: list[str] | None = None,
    fields: Any = None,
    ack_deadline: datetime | None = None,
    knowledge_key: str | None = "kk-test",
    with_refs: bool = True,
    raw_event: dict[str, Any] | None = None,
    first_event_received_at: datetime | None = None,
) -> asyncpg.Record:
    if with_refs:
        await ensure_source_and_rule(pool, team_slug)
    alert_id = uuid7()
    now = datetime.now(UTC)
    return await pool.fetchrow(
        "INSERT INTO alerts (id, team_id, domain_id, rule_id, source_id, fingerprint, knowledge_key, title, "
        "body, severity, status, count, first_seen_at, last_seen_at, ack_deadline, flags, fields, raw_event, "
        "first_event_received_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'Body', $9, $10, $11, $12, $13, $14, $15, $16, $17, $18) "
        "RETURNING id, short_id, team_id, domain_id",
        alert_id,
        team(team_slug),
        domain(domain_slug),
        RULE_ID if with_refs else None,
        SOURCE_ID if with_refs else None,
        f"fp-{alert_id}",
        knowledge_key,
        title,
        severity,
        status,
        count,
        now,
        last_seen_at or now,
        ack_deadline,
        flags or [],
        fields if fields is not None else {"Service": "payments-api", "Env": "prod"},
        raw_event or {"type": "TEST", "n": 1},
        first_event_received_at,
    )


async def make_delivery(
    pool: asyncpg.Pool,
    alert_id: UUID,
    *,
    channel: str = "sms",
    kind: str = "hard",
    status: str = "sent",
    user_email: str | None = "anna@demo",
    event_received_at: datetime | None = None,
    delivered_at: datetime | None = None,
    attempts: int = 1,
) -> UUID:
    delivery_id = uuid7()
    await pool.execute(
        "INSERT INTO deliveries (id, alert_id, recipient_user_id, recipient_address, channel, kind, "
        "idempotency_key, status, attempts, event_received_at, delivered_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        delivery_id,
        alert_id,
        user(user_email) if user_email else None,
        None if user_email else "+48500000000",
        channel,
        kind,
        f"idem-{delivery_id}",
        status,
        attempts,
        event_received_at,
        delivered_at,
    )
    return delivery_id
