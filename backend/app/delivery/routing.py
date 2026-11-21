"""Delivery planning (spec 9): who is notified, on which channel, with which deadline.

`plan_deliveries` runs inside the caller's transaction and inserts `deliveries` plus `outbox` rows; the
caller publishes the returned `outbox_id`s after commit (`publish_outbox_rows`).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg

from app.core.ids import uuid7
from app.core.outbox import add_outbox
from app.core.redis import get_redis, rkey
from app.core.security import decrypt_secret
from app.core.timeline import add_timeline
from app.delivery.channel_select import select_channels
from app.delivery.recipients import Person, domain_roots, masters, resolve_specs
from app.delivery.ruleview import RuleView, parse_specs, rule_view
from app.escalation.policies import EffectivePolicy, effective_policy, scaled_seconds

log = logging.getLogger(__name__)

Reason = Literal["created", "escalated", "renotify", "resolved", "severity_up", "reopened"]
SEVERITY_PRIORITY = {"critical": 9, "error": 6, "warning": 3, "info": 0}
ADDRESS_FALLBACK = {"phone": "sms", "sms": "email"}
STATUS_PENDING = "pending"
STATUS_SUPPRESSED = "suppressed_digest"


@dataclass
class _Pending:
    person: Person | None
    channel: str
    kind: str
    address: str | None
    who: str
    secret: str | None = None


async def _level_recipients(
    conn: asyncpg.Connection,
    rv: RuleView,
    policy: EffectivePolicy,
    alert: dict[str, Any],
    level: int,
    at: datetime,
) -> list[Person]:
    """Hard recipients for `level`. Informational rules (no hard list) have none at any level."""
    if not rv.hard:
        return []
    team_id, domain_id = alert["team_id"], alert["domain_id"]
    if level <= 1:
        people = await resolve_specs(conn, rv.hard, team_id=team_id, domain_id=domain_id, at=at)
        return people or await domain_roots(conn, domain_id)
    if rv.escalation_path and level - 2 < len(rv.escalation_path):
        return await resolve_specs(
            conn, [rv.escalation_path[level - 2]], team_id=team_id, domain_id=domain_id, at=at
        )
    lvl = policy.level_for(level)
    target = lvl.target.value if lvl else "domain_root"
    if target == "oncall":
        return await resolve_specs(
            conn, parse_specs(["oncall:" + str(team_id)]), team_id=team_id, domain_id=domain_id, at=at
        )
    if target == "master":
        return await masters(conn)
    if target == "custom" and lvl and lvl.recipients:
        specs = parse_specs([{"kind": r.kind.value, "id": r.id} for r in lvl.recipients])
        return await resolve_specs(conn, specs, team_id=team_id, domain_id=domain_id, at=at)
    return await domain_roots(conn, domain_id)


async def _preferences(conn: asyncpg.Connection, people: list[Person]) -> dict[UUID, dict[str, Any]]:
    if not people:
        return {}
    rows = await conn.fetch(
        "SELECT user_id, by_severity, working_hours FROM preferences WHERE user_id = ANY($1)",
        [p.id for p in people],
    )
    return {r["user_id"]: dict(r) for r in rows}


async def _throttled(recipient: str, channel: str, now: datetime, limit: int) -> bool:
    """Per recipient/channel/minute counter (spec 8). Redis trouble never blocks a notification."""
    key = rkey("rl", recipient, channel, str(int(now.timestamp() // 60)))
    try:
        pipe = get_redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, 120)
        count = (await pipe.execute())[0]
    except Exception:
        log.warning("throttle counter unavailable; not throttling", exc_info=True)
        return False
    return count > limit


def _address(person: Person, channel: str) -> tuple[str, str | None]:
    """Channel actually usable for the person (degrading phone -> sms -> email) and its address."""
    while channel in ADDRESS_FALLBACK:
        if person.phone:
            return channel, person.phone
        channel = ADDRESS_FALLBACK[channel]
    return channel, person.email


async def _webhook_targets(conn: asyncpg.Connection, team_id: UUID) -> list[_Pending]:
    rows = await conn.fetch(
        "SELECT url, secret_encrypted FROM webhook_subscriptions WHERE team_id = $1 "
        "AND (cardinality(event_types) = 0 OR 'alert.notification' = ANY(event_types))",
        team_id,
    )
    out = []
    for r in rows:
        secret = decrypt_secret(r["secret_encrypted"]) if r["secret_encrypted"] else None
        who = "hook-" + hashlib.sha1(r["url"].encode()).hexdigest()[:8]
        out.append(_Pending(None, "webhook", "soft", r["url"], who, secret))
    return out


def _expand(
    people: list[Person],
    kind: str,
    prefs: dict[UUID, dict[str, Any]],
    *,
    severity: str,
    rv: RuleView,
    policy: EffectivePolicy,
    level: int,
    now: datetime,
) -> tuple[list[_Pending], bool, bool]:
    """(per-person deliveries, any wants ui, any wants webhook); ui/webhook collapse to team-level ones."""
    pending: list[_Pending] = []
    want_ui = want_webhook = False
    for person in people:
        channels = select_channels(
            kind=kind,
            severity=severity,
            rule=rv,
            prefs=prefs.get(person.id),
            policy=policy,
            level=level,
            now=now,
        )
        seen: set[str] = set()
        for channel in channels:
            if channel == "ui":
                want_ui = True
            elif channel == "webhook":
                want_webhook = True
            else:
                actual, address = _address(person, channel)
                if actual not in seen and address:
                    seen.add(actual)
                    pending.append(_Pending(person, actual, kind, address, str(person.id)))
    return pending, want_ui, want_webhook


async def plan_deliveries(
    conn: asyncpg.Connection,
    alert: dict[str, Any],
    rule: Any,
    *,
    reason: Reason,
    level: int = 1,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Plan and persist the deliveries for one alert state change.

    `alert` is an `alerts` row (snake_case), `rule` a RuleDef or its camelCase definition (or None).
    Returns the inserted rows as dicts (`id`, `channel`, `kind`, `status`, `recipient_user_id`,
    `idempotency_key`, `notification_seq`, `outbox_id`; `outbox_id` is None for suppressed deliveries).
    For `created` / `escalated` the alert's `ack_deadline` / `next_renotify_at` are set from the effective
    policy (only when the rule has a hard list); `renotify` re-arms `next_renotify_at`.
    """
    alert = dict(alert)
    rv = rule_view(rule)
    now = now or datetime.now(UTC)
    severity = alert["severity"]
    policy = await effective_policy(
        conn, rule=rule, team_id=alert["team_id"], domain_id=alert["domain_id"], severity=severity
    )

    hard: list[Person] = []
    soft: list[Person] = []
    if reason == "resolved":
        everyone = await resolve_specs(
            conn, [*rv.hard, *rv.soft], team_id=alert["team_id"], domain_id=alert["domain_id"], at=now
        )
        soft = everyone
    else:
        hard = await _level_recipients(conn, rv, policy, alert, level, now)
        if reason != "renotify":
            hard_ids = {p.id for p in hard}
            soft = [
                p
                for p in await resolve_specs(
                    conn, rv.soft, team_id=alert["team_id"], domain_id=alert["domain_id"], at=now
                )
                if p.id not in hard_ids
            ]

    prefs = await _preferences(conn, [*hard, *soft])
    common = {"severity": severity, "rv": rv, "policy": policy, "level": level, "now": now}
    plan: list[_Pending] = []
    want_ui = want_webhook = False
    for people, kind in ((hard, "hard"), (soft, "soft")):
        got, ui, hook = _expand(people, kind, prefs, **common)
        plan += got
        want_ui, want_webhook = want_ui or ui, want_webhook or hook
    if want_webhook:
        plan += await _webhook_targets(conn, alert["team_id"])
    if reason != "renotify":
        plan.append(_Pending(None, "ui", "hard" if hard else "soft", None, "team"))

    alert_id = alert["id"]
    seq = await conn.fetchval(
        "SELECT COALESCE(MAX(notification_seq), 0) + 1 FROM deliveries WHERE alert_id = $1", alert_id
    )
    # End-to-end latency (spec §18) is defined for the first notification of an alert. Later
    # notifications (escalation, renotify, resolved updates) are deliberately delayed, so they carry no
    # event_received_at and stay out of the latency metrics.
    received_at = (
        alert.get("first_event_received_at") or alert.get("first_seen_at") if reason == "created" else None
    )
    priority = SEVERITY_PRIORITY.get(severity, 0)
    digest = policy.soft_digest_minutes
    created: list[dict[str, Any]] = []
    for item in plan:
        suppressed = False
        if item.person is not None:
            suppressed = await _throttled(item.who, item.channel, now, rv.max_per_minute)
            if not suppressed and item.kind == "soft" and digest and item.channel == "email":
                suppressed = True
        status = STATUS_SUPPRESSED if suppressed else STATUS_PENDING
        delivery_id = uuid7()
        key = f"{alert_id}:{seq}:{item.who}:{item.channel}"
        inserted = await conn.fetchval(
            "INSERT INTO deliveries (id, alert_id, recipient_user_id, recipient_address, channel, kind, "
            "notification_seq, idempotency_key, status, priority, event_received_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
            delivery_id,
            alert_id,
            item.person.id if item.person else None,
            item.address,
            item.channel,
            item.kind,
            seq,
            key,
            status,
            priority,
            received_at,
        )
        if inserted is None:
            continue
        outbox_id = None
        if not suppressed:
            outbox_id = await add_outbox(
                conn, "deliveries", item.channel, {"deliveryId": str(delivery_id)}, priority
            )
        created.append(
            {
                "id": delivery_id,
                "alert_id": alert_id,
                "channel": item.channel,
                "kind": item.kind,
                "status": status,
                "recipient_user_id": item.person.id if item.person else None,
                "recipient_address": item.address,
                "idempotency_key": key,
                "notification_seq": seq,
                "outbox_id": outbox_id,
            }
        )

    suppressed_count = sum(1 for d in created if d["status"] == STATUS_SUPPRESSED)
    if suppressed_count:
        await add_timeline(
            conn,
            alert_id,
            "notified",
            f"{suppressed_count} notification(s) held back (throttled or sent as digest)",
            data={"suppressed": suppressed_count, "reason": reason},
        )
    await _arm_timers(conn, alert, policy, hard, reason=reason, level=level, now=now)
    return created


async def _arm_timers(
    conn: asyncpg.Connection,
    alert: dict[str, Any],
    policy: EffectivePolicy,
    hard: list[Person],
    *,
    reason: str,
    level: int,
    now: datetime,
) -> None:
    if not hard or reason not in ("created", "escalated", "renotify"):
        return
    renotify_at = (
        now + timedelta(seconds=scaled_seconds(policy.renotify_seconds)) if policy.renotify_seconds else None
    )
    if reason == "renotify":
        await conn.execute("UPDATE alerts SET next_renotify_at = $2 WHERE id = $1", alert["id"], renotify_at)
        return
    lvl = policy.level_for(level)
    ack = lvl.ackTimeoutSeconds if lvl else None
    deadline = now + timedelta(seconds=scaled_seconds(ack)) if ack else None
    await conn.execute(
        "UPDATE alerts SET ack_deadline = $2, next_renotify_at = $3 WHERE id = $1",
        alert["id"],
        deadline,
        renotify_at,
    )
