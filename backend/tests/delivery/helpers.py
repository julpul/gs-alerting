"""Shared helpers for the delivery tests: ids of seeded rows, alert/rule factories, polling."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.ids import stable_id, uuid7

MAILPIT = "http://localhost:8025/api/v1"


def team(slug: str) -> uuid.UUID:
    return stable_id("team", slug)


def domain(slug: str) -> uuid.UUID:
    return stable_id("domain", slug)


def user(email: str) -> uuid.UUID:
    return stable_id("user", email)


def team_domain(slug: str) -> str:
    return {
        "team-payments": "payments",
        "team-settlements": "settlements",
        "team-security": "security",
        "team-pricing": "markets",
        "team-rates": "markets",
    }[slug]


async def make_alert(
    pool,
    team_slug: str = "team-settlements",
    *,
    severity: str = "critical",
    title: str | None = None,
    status: str = "open",
    escalation_level: int = 1,
) -> dict[str, Any]:
    """Insert an `alerts` row (no rule) and return it as a dict."""
    row = await pool.fetchrow(
        "INSERT INTO alerts (id, team_id, domain_id, fingerprint, title, body, severity, status, "
        "escalation_level, first_event_received_at, fields) "
        "VALUES ($1, $2, $3, $4, $5, 'body text', $6, $7, $8, $9, $10) RETURNING *",
        uuid7(),
        team(team_slug),
        domain(team_domain(team_slug)),
        uuid.uuid4().hex,
        title or f"Test alert {uuid.uuid4().hex[:8]}",
        severity,
        status,
        escalation_level,
        datetime.now(UTC),
        {"step": "validate-counterparty", "workflowId": "wf-1"},
    )
    return dict(row)


def rule_def(
    *,
    hard: list[dict] | None = None,
    soft: list[dict] | None = None,
    channels: dict[str, list[str]] | None = None,
    time_window: dict | None = None,
    max_per_minute: int | None = None,
    ack_timeouts: dict[str, int] | None = None,
    path: list[dict] | None = None,
    rule_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    routing: dict[str, Any] = {"hard": hard or [], "soft": soft or []}
    if channels:
        routing["channelsBySeverity"] = channels
    if time_window:
        routing["timeWindow"] = time_window
    out: dict[str, Any] = {"id": str(rule_id or uuid7()), "name": "t", "routing": routing}
    if max_per_minute:
        out["throttle"] = {"maxPerRecipientPerMinute": max_per_minute}
    if ack_timeouts or path:
        out["escalation"] = {}
        if ack_timeouts:
            out["escalation"]["ackTimeoutSecondsBySeverity"] = ack_timeouts
        if path:
            out["escalation"]["path"] = path
    return out


async def plan(pool, alert: dict, rule: Any, **kwargs) -> list[dict]:
    from app.delivery.routing import plan_deliveries

    async with pool.acquire() as conn, conn.transaction():
        return await plan_deliveries(conn, alert, rule, **kwargs)


async def wait_for(
    check: Callable[[], Awaitable[Any]],
    *,
    wait_s: float = 10.0,
    interval: float = 0.1,
    what: str = "condition",
) -> Any:
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        value = await check()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(interval)


async def mailpit_search(query: str, *, wait_s: float = 10.0) -> list[dict]:
    async with httpx.AsyncClient(timeout=5) as client:

        async def found() -> list[dict]:
            resp = await client.get(f"{MAILPIT}/search", params={"query": query})
            return resp.json()["messages"]

        return await wait_for(found, wait_s=wait_s, what=f"mailpit message for {query!r}")
