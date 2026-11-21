"""Automatic incident manager for every new alert: a small, explainable if/else decision model.

First hit wins (`decide`):
  1. the rule names a person as a hard recipient (`routing.hard` entry of kind `user`)
     -> that person, "named in rule <rule name>";
  2. the team's on-call from real shifts covering now (not the "whole team" fallback); with several, the
     one with the fewest open assigned alerts in the team -> "on call for <team name>";
  3. the team member with the fewest open assigned alerts in the team -> "least busy in <team name>";
  4. nobody eligible (e.g. a team without members) -> the alert stays unassigned and a warning is logged.
Eligible people can see the alert's team and may act on it (master, domain_root of one of the team's
domains, the member role or a team membership), so `informed`-only people are never chosen. Ties keep the
stable order: rule order, shift order, e-mail. The data model has no team-lead role, so there is no lead
tie-break.

The assignment is a conditional UPDATE (`assignee_id IS NULL AND assigned_by IS NULL`): a person who
assigned or released the alert first always wins, and it is never repeated for the same alert.
Backfill for open alerts created before this existed: `uv run python -m app.escalation.auto_assign`
(the scheduler also runs it once at start-up).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.core.audit import audit
from app.core.timeline import add_timeline
from app.delivery.recipients import resolve_spec
from app.delivery.ruleview import rule_view
from app.escalation.oncall import oncall_from_shifts
from app.pipeline.ruleview import cfg

log = logging.getLogger(__name__)

AUTO_ACTOR: dict[str, Any] = {"type": "system", "id": None, "name": "auto-assign", "onBehalfOf": None}

_CANDIDATES = """
SELECT u.id, u.display_name,
    EXISTS (
        SELECT 1 FROM role_assignments r WHERE r.user_id = u.id AND (
            r.role = 'master'
            OR (r.role = 'domain_root' AND r.scope_type = 'domain' AND r.scope_id IN (
                SELECT domain_id FROM team_domains WHERE team_id = $2 UNION SELECT $3::uuid))
            OR (r.role = 'member' AND r.scope_type = 'team' AND r.scope_id = $2))
    ) OR EXISTS (SELECT 1 FROM team_members m WHERE m.user_id = u.id AND m.team_id = $2) AS eligible,
    (SELECT count(*) FROM alerts a
     WHERE a.team_id = $2 AND a.status <> 'resolved' AND a.assignee_id = u.id) AS open_alerts
FROM users u WHERE u.id = ANY($1::uuid[])
"""


@dataclass(frozen=True)
class Candidate:
    id: UUID
    name: str
    open_alerts: int = 0
    eligible: bool = True


@dataclass(frozen=True)
class Facts:
    rule_name: str
    team_name: str
    named: Sequence[Candidate] = ()
    oncall: Sequence[Candidate] = ()
    members: Sequence[Candidate] = ()


@dataclass(frozen=True)
class Decision:
    user_id: UUID
    name: str
    reason: str


def _least_busy(candidates: Sequence[Candidate]) -> Candidate | None:
    """Fewest open assigned alerts; `min` keeps the first of equals, so ties follow the given order."""
    return min((c for c in candidates if c.eligible), key=lambda c: c.open_alerts, default=None)


def decide(facts: Facts) -> Decision | None:
    """The decision model (pure): who owns the alert and why, or None when nobody is eligible."""
    named = next((c for c in facts.named if c.eligible), None)
    if named is not None:
        return Decision(named.id, named.name, f"named in rule {facts.rule_name}")
    oncall = _least_busy(facts.oncall)
    if oncall is not None:
        return Decision(oncall.id, oncall.name, f"on call for {facts.team_name}")
    member = _least_busy(facts.members)
    if member is not None:
        return Decision(member.id, member.name, f"least busy in {facts.team_name}")
    return None


async def _named_user_ids(conn: asyncpg.Connection, rule: Any, alert: Mapping[str, Any]) -> list[UUID]:
    ids: list[UUID] = []
    for spec in rule_view(rule).hard:
        if spec.kind != "user":
            continue
        people = await resolve_spec(conn, spec, team_id=alert["team_id"], domain_id=alert["domain_id"])
        ids += [p.id for p in people if p.id not in ids]
    return ids


async def gather_facts(
    conn: asyncpg.Connection, alert: Mapping[str, Any], rule: Any, *, at: datetime | None = None
) -> Facts:
    """Everything `decide` needs, in a handful of reads (no per-candidate scope loading)."""
    team_id = alert["team_id"]
    named = await _named_user_ids(conn, rule, alert)
    oncall = await oncall_from_shifts(conn, team_id, at)
    members = [
        r["user_id"]
        for r in await conn.fetch(
            "SELECT m.user_id FROM team_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.team_id = $1 ORDER BY u.email",
            team_id,
        )
    ]
    rows = await conn.fetch(_CANDIDATES, list({*named, *oncall, *members}), team_id, alert["domain_id"])
    by_id = {r["id"]: Candidate(r["id"], r["display_name"], r["open_alerts"], r["eligible"]) for r in rows}
    team_name = await conn.fetchval("SELECT name FROM teams WHERE id = $1", team_id) or str(team_id)
    return Facts(
        rule_name=cfg(rule, "name") or "of this alert",
        team_name=team_name,
        named=[by_id[i] for i in named if i in by_id],
        oncall=[by_id[i] for i in oncall if i in by_id],
        members=[by_id[i] for i in members if i in by_id],
    )


async def auto_assign(
    conn: asyncpg.Connection, alert: Mapping[str, Any], rule: Any, *, at: datetime | None = None
) -> Decision | None:
    """Decide and record (timeline `assigned` + audit `alert.auto_assign`) in the caller's transaction.

    Returns None when nobody is eligible or a person already assigned / released the alert."""
    facts = await gather_facts(conn, alert, rule, at=at)
    decision = decide(facts)
    if decision is None:
        log.warning("alert %s left unassigned: nobody eligible in %s", alert["id"], facts.team_name)
        return None
    updated = await conn.fetchval(
        "UPDATE alerts SET assignee_id = $2, assigned_by = $3, assign_reason = $4 "
        "WHERE id = $1 AND assignee_id IS NULL AND assigned_by IS NULL RETURNING id",
        alert["id"],
        decision.user_id,
        AUTO_ACTOR,
        decision.reason,
    )
    if updated is None:
        return None
    data = {"assigneeId": str(decision.user_id), "reason": decision.reason, "auto": True}
    await add_timeline(
        conn,
        alert["id"],
        "assigned",
        f"Assigned to {decision.name} automatically: {decision.reason}",
        actor=AUTO_ACTOR,
        data=data,
    )
    await audit(conn, AUTO_ACTOR, "alert.auto_assign", "alert", str(alert["id"]), data)
    return decision


async def assign_new_alert(conn: asyncpg.Connection, alert: Mapping[str, Any], rule: Any) -> Decision | None:
    """For the alert writer: runs in a savepoint and never fails the alert (errors are logged)."""
    try:
        async with conn.transaction():
            return await auto_assign(conn, alert, rule)
    except Exception:
        log.exception("automatic assignment failed for alert %s", alert["id"])
        return None


async def _rule(conn: asyncpg.Connection, rule_id: UUID | None) -> dict[str, Any] | None:
    if rule_id is None:
        return None
    row = await conn.fetchrow("SELECT name, team_id, definition FROM rules WHERE id = $1", rule_id)
    if row is None:
        return None
    return {**(row["definition"] or {}), "id": rule_id, "teamId": row["team_id"], "name": row["name"]}


async def backfill_open(pool: asyncpg.Pool, team_ids: Sequence[UUID] | None = None) -> int:
    """Assign every open alert nobody ever (un)assigned, oldest first (optionally only these teams'
    alerts); returns how many were assigned."""
    from app.core.sse import publish_sse
    from app.pipeline.alert_writer import alert_summary

    pending = await pool.fetch(
        "SELECT id FROM alerts WHERE status <> 'resolved' AND assignee_id IS NULL AND assigned_by IS NULL "
        "AND ($1::uuid[] IS NULL OR team_id = ANY($1)) ORDER BY first_seen_at, id",
        list(team_ids) if team_ids is not None else None,
    )
    assigned = 0
    for item in pending:
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM alerts WHERE id = $1 AND status <> 'resolved' AND assignee_id IS NULL "
                "AND assigned_by IS NULL FOR UPDATE SKIP LOCKED",
                item["id"],
            )
            if row is None:
                continue
            decision = await auto_assign(conn, row, await _rule(conn, row["rule_id"]))
            fresh = await conn.fetchrow("SELECT * FROM alerts WHERE id = $1", row["id"])
        if decision is None:
            continue
        assigned += 1
        try:
            await publish_sse("alert.updated", alert_summary(dict(fresh)), team_ids=[fresh["team_id"]])
        except Exception:
            log.warning("SSE update after automatic assignment failed", exc_info=True)
    if assigned:
        log.info("assigned %d open alert(s) automatically", assigned)
    return assigned


async def main() -> None:
    from app.core.db import create_pool, run_migrations
    from app.core.redis import close_redis

    pool = await create_pool()
    try:
        await run_migrations(pool)
        print(f"assigned automatically: {await backfill_open(pool)}")
    finally:
        await pool.close()
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
