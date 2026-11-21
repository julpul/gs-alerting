"""Factories for the runbook tests."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from app.core.ids import stable_id, uuid7

WORKFLOW_FAILED = {
    "type": "WORKFLOW_FAILED",
    "workflowId": "WF-20261023-00042",
    "workflowName": "order-settlement",
    "step": "validate-counterparty",
    "severity": "ERROR",
    "environment": "prod",
    "timestamp": "2026-10-23T09:14:02.331Z",
    "error": {"code": "TIMEOUT", "message": "Counterparty service did not respond in 5000 ms"},
}
DOMAIN_OF = {
    "team-payments": "payments",
    "team-settlements": "settlements",
    "team-pricing": "markets",
    "team-security": "security",
}


def team(slug: str) -> uuid.UUID:
    return stable_id("team", slug)


def user(email: str) -> uuid.UUID:
    return stable_id("user", email)


def runbook_id(key: str) -> uuid.UUID:
    return stable_id("runbook", key)


async def make_alert(
    pool: asyncpg.Pool,
    *,
    team_slug: str = "team-settlements",
    raw: dict[str, Any] | None = None,
    severity: str = "critical",
    runbook: uuid.UUID | None = None,
    rule_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """An alert row (as dict) carrying `raw` as its raw event; settlements WORKFLOW_FAILED by default."""
    row = await pool.fetchrow(
        "INSERT INTO alerts (id, team_id, domain_id, rule_id, fingerprint, title, body, severity, "
        "raw_event, runbook_id, first_event_received_at) "
        "VALUES ($1, $2, $3, $4, $5, 'Settlement failed', 'body', $6, $7, $8, now()) RETURNING *",
        uuid7(),
        team(team_slug),
        stable_id("domain", DOMAIN_OF[team_slug]),
        rule_id,
        uuid.uuid4().hex,
        severity,
        raw if raw is not None else WORKFLOW_FAILED,
        runbook,
    )
    return dict(row)
