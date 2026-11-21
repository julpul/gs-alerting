"""Scheduler jobs (spec 10.2). Each job claims its rows with `FOR UPDATE SKIP LOCKED` in one short
transaction, so any number of scheduler replicas can run them without double work. Outbox rows and SSE
events are published after the transaction committed.
"""

from __future__ import annotations

import html
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.config import get_settings
from app.core.ids import format_short_id
from app.core.outbox import publish_outbox_rows
from app.core.rabbit import Rabbit
from app.core.sse import publish_sse
from app.core.timeline import add_timeline
from app.delivery.channels.base import ChannelError, Message
from app.delivery.channels.registry import get_adapter
from app.delivery.routing import plan_deliveries
from app.delivery.ruleview import rule_view
from app.escalation import rotations
from app.escalation.policies import EffectivePolicy, effective_policy
from app.repositories.alert_actions import _load_rule as load_rule
from app.repositories.alerts import SUMMARY_SELECT, summary_out

log = logging.getLogger(__name__)

BATCH = 100
RETRY_AFTER_FAILURE_S = 10.0
OPEN_STATUSES = "('open', 'escalated')"
TARGET_LABELS = {
    "oncall": "On-call",
    "domain_root": "Domain Root",
    "master": "Master",
    "custom": "custom recipients",
}
SEVERITY_TAG = {"critical": "CRIT", "error": "ERR", "warning": "WARN", "info": "INFO"}


def human_duration(seconds: int | None) -> str:
    if not seconds:
        return "no ack"
    return f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s"


def _outbox_ids(created: list[dict[str, Any]]) -> list[int]:
    return [d["outbox_id"] for d in created if d.get("outbox_id") is not None]


async def _publish_outbox(pool: asyncpg.Pool, rabbit: Rabbit, ids: list[int]) -> None:
    try:
        await publish_outbox_rows(pool, rabbit, ids)
    except Exception:
        log.exception("outbox publish failed; relay will retry")


def _target_label(policy: EffectivePolicy, rule: Any, new_level: int) -> tuple[str, int | None]:
    """(label of who gets level `new_level`, ack timeout of the level before it in seconds)."""
    path = rule_view(rule).escalation_path
    previous = policy.level_for(new_level - 1)
    timeout = previous.ackTimeoutSeconds if previous else None
    if path and new_level - 2 < len(path):
        kind = path[new_level - 2].kind
        return TARGET_LABELS.get(kind, kind), timeout
    level = policy.level_for(new_level)
    return TARGET_LABELS.get(level.target.value if level else "domain_root", "Domain Root"), timeout


def _max_level(policy: EffectivePolicy, rule: Any) -> int:
    chain = policy.levels[-1].level if policy.levels else 1
    return max(chain, 1 + len(rule_view(rule).escalation_path))


async def _escalate_one(conn: asyncpg.Connection, alert: dict[str, Any]) -> list[int] | None:
    """Move one alert up a level. Returns outbox ids, or None when it is already at the last level."""
    rule = await load_rule(conn, alert)
    policy = await effective_policy(
        conn, rule=rule, team_id=alert["team_id"], domain_id=alert["domain_id"], severity=alert["severity"]
    )
    new_level = alert["escalation_level"] + 1
    if new_level > _max_level(policy, rule):
        await conn.execute("UPDATE alerts SET ack_deadline = NULL WHERE id = $1", alert["id"])
        return None
    label, timeout = _target_label(policy, rule, new_level)
    await conn.execute(
        "UPDATE alerts SET escalation_level = $2, status = 'escalated', ack_deadline = NULL WHERE id = $1",
        alert["id"],
        new_level,
    )
    escalated = {**alert, "escalation_level": new_level, "status": "escalated", "ack_deadline": None}
    created = await plan_deliveries(conn, escalated, rule, reason="escalated", level=new_level)
    names = await conn.fetch(
        "SELECT display_name FROM users WHERE id = ANY($1) ORDER BY display_name",
        list({d["recipient_user_id"] for d in created if d["kind"] == "hard" and d["recipient_user_id"]}),
    )
    await add_timeline(
        conn,
        alert["id"],
        "escalated",
        f"Escalated to {label} (no ack in {human_duration(timeout)})",
        data={"level": new_level, "target": label, "notified": [r["display_name"] for r in names]},
    )
    return _outbox_ids(created)


async def _publish_escalated(pool: asyncpg.Pool, alert_ids: list[UUID]) -> None:
    rows = await pool.fetch(f"{SUMMARY_SELECT} WHERE a.id = ANY($1)", alert_ids)
    for row in rows:
        summary = summary_out(row)
        try:
            await publish_sse(
                "alert.escalated",
                {**summary, "alertId": summary["id"], "level": row["escalation_level"]},
                team_ids=[row["team_id"]],
            )
        except Exception:
            log.exception("SSE publish of alert.escalated failed")


async def escalate_due(pool: asyncpg.Pool, rabbit: Rabbit, *, limit: int = BATCH) -> int:
    """Escalate alerts whose ack deadline passed; returns how many moved up a level."""
    outbox: list[int] = []
    escalated: list[UUID] = []
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "SELECT * FROM alerts WHERE status IN ('open', 'escalated') AND ack_deadline <= now() "
            "ORDER BY ack_deadline LIMIT $1 FOR UPDATE SKIP LOCKED",
            limit,
        )
        for row in rows:
            alert = dict(row)
            try:
                async with conn.transaction():
                    ids = await _escalate_one(conn, alert)
            except Exception:
                log.exception("escalation of alert %s failed; retrying shortly", alert["id"])
                await conn.execute(
                    "UPDATE alerts SET ack_deadline = now() + make_interval(secs => $2) WHERE id = $1",
                    alert["id"],
                    RETRY_AFTER_FAILURE_S,
                )
                continue
            if ids is not None:
                outbox += ids
                escalated.append(alert["id"])
    await _publish_outbox(pool, rabbit, outbox)
    if escalated:
        await _publish_escalated(pool, escalated)
    return len(escalated)


async def renotify_due(pool: asyncpg.Pool, rabbit: Rabbit, *, limit: int = BATCH) -> int:
    """Repeat the hard notification for alerts that are still not acknowledged."""
    outbox: list[int] = []
    renotified = 0
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "SELECT * FROM alerts WHERE status IN ('open', 'escalated') AND next_renotify_at <= now() "
            "ORDER BY next_renotify_at LIMIT $1 FOR UPDATE SKIP LOCKED",
            limit,
        )
        for row in rows:
            alert = dict(row)
            try:
                async with conn.transaction():
                    await conn.execute("UPDATE alerts SET next_renotify_at = NULL WHERE id = $1", alert["id"])
                    created = await plan_deliveries(
                        conn,
                        {**alert, "next_renotify_at": None},
                        await load_rule(conn, alert),
                        reason="renotify",
                        level=alert["escalation_level"],
                    )
                    await add_timeline(
                        conn,
                        alert["id"],
                        "notified",
                        f"Renotified (level {alert['escalation_level']}, still no ack)",
                        data={"reason": "renotify", "level": alert["escalation_level"]},
                    )
            except Exception:
                log.exception("renotify of alert %s failed; retrying shortly", alert["id"])
                await conn.execute(
                    "UPDATE alerts SET next_renotify_at = now() + make_interval(secs => $2) WHERE id = $1",
                    alert["id"],
                    RETRY_AFTER_FAILURE_S,
                )
                continue
            outbox += _outbox_ids(created)
            renotified += 1
    await _publish_outbox(pool, rabbit, outbox)
    return renotified


async def expire_approvals(pool: asyncpg.Pool, *, limit: int = BATCH) -> int:
    """Pending approvals past `expires_at` become `expired` (timeline + SSE `approval.decided`).

    Delegates to the approvals module when it exists (it also wakes waiting agents and skips step runs);
    otherwise the rows are updated here."""
    try:
        from app.runbooks.approvals import expire_due
    except ImportError:
        return await _expire_approvals_directly(pool, limit=limit)
    async with pool.acquire() as conn, conn.transaction():
        return len(await expire_due(conn))


async def _expire_approvals_directly(pool: asyncpg.Pool, *, limit: int) -> int:
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "UPDATE approvals SET status = 'expired' WHERE id IN ("
            "SELECT id FROM approvals WHERE status = 'pending' AND expires_at <= now() "
            "ORDER BY expires_at LIMIT $1 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, short_id, alert_id, action, approver_user_ids",
            limit,
        )
        teams: dict[UUID, UUID] = {}
        for row in rows:
            if row["alert_id"] is None:
                continue
            team = await conn.fetchval("SELECT team_id FROM alerts WHERE id = $1", row["alert_id"])
            if team is not None:
                teams[row["id"]] = team
                await add_timeline(
                    conn,
                    row["alert_id"],
                    "approval_decided",
                    f"Approval {row['short_id']} for {row['action']} expired without a decision",
                    data={"approvalId": str(row["id"]), "status": "expired"},
                )
    for row in rows:
        payload = {
            "approvalId": str(row["id"]),
            "id": str(row["id"]),
            "shortId": row["short_id"],
            "status": "expired",
            "alertId": str(row["alert_id"]) if row["alert_id"] else None,
        }
        try:
            await publish_sse(
                "approval.decided",
                payload,
                team_ids=[teams[row["id"]]] if row["id"] in teams else [],
                user_ids=row["approver_user_ids"] or [],
            )
        except Exception:
            log.exception("SSE publish of approval.decided failed")
    return len(rows)


async def materialise_shifts(pool: asyncpg.Pool, *, now: datetime | None = None) -> int:
    async with pool.acquire() as conn, conn.transaction():
        return await rotations.materialise_all(conn, now=now)


def _digest_message(email: str, name: str, items: list[dict[str, Any]]) -> Message:
    base = get_settings().public_base_url.rstrip("/")
    lines, rows = [], []
    for item in items:
        sid = format_short_id(item["short_id"])
        tag = SEVERITY_TAG.get(item["severity"], item["severity"].upper())
        line = f"[{tag}] {sid} {item['title']} ({item['n']} held back)"
        lines.append(f"{line} {base}/a/{item['short_id']}")
        rows.append(
            f'<li><b>[{tag}]</b> <a href="{html.escape(base)}/a/{item["short_id"]}">{sid}</a> '
            f"{html.escape(item['title'])} ({item['n']} held back)</li>"
        )
    total = sum(i["n"] for i in items)
    subject = f"Alert digest: {total} notification(s) on {len(items)} alert(s)"
    text = f"Hi {name},\n\nThese notifications were held back (throttled or digest-only):\n\n" + "\n".join(
        lines
    )
    markup = (
        f"<p>Hi {html.escape(name)},</p><p>These notifications were held back (throttled or digest-only):</p>"
        f"<ul>{''.join(rows)}</ul>"
    )
    return Message(channel="email", to=email, text=text, subject=subject, html=markup)


async def send_digests(pool: asyncpg.Pool, *, limit: int = 500) -> int:
    """One email per recipient for their `suppressed_digest` deliveries, which then become `digested`."""
    sent = 0
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "SELECT d.id, d.recipient_user_id, d.alert_id, a.short_id, a.title, a.severity, "
            "u.email, u.display_name FROM deliveries d JOIN alerts a ON a.id = d.alert_id "
            "JOIN users u ON u.id = d.recipient_user_id WHERE d.status = 'suppressed_digest' "
            "ORDER BY d.created_at LIMIT $1 FOR UPDATE OF d SKIP LOCKED",
            limit,
        )
        by_user: dict[UUID, list[asyncpg.Record]] = defaultdict(list)
        for row in rows:
            by_user[row["recipient_user_id"]].append(row)
        email = get_adapter("email")
        for user_id, user_rows in by_user.items():
            per_alert: dict[UUID, dict[str, Any]] = {}
            for r in user_rows:
                item = per_alert.setdefault(r["alert_id"], {**dict(r), "n": 0})
                item["n"] += 1
            first = user_rows[0]
            message = _digest_message(first["email"], first["display_name"], list(per_alert.values()))
            try:
                await email.send(message, f"digest:{user_id}:{first['id']}")
            except (ChannelError, OSError):
                log.exception("digest email for user %s failed; deliveries stay queued", user_id)
                continue
            await conn.execute(
                "UPDATE deliveries SET status = 'digested', sent_at = now() WHERE id = ANY($1)",
                [r["id"] for r in user_rows],
            )
            sent += 1
    return sent
