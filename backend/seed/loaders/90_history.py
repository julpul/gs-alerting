"""Seed ~40 resolved incidents of the last 30 days ("fixed before" demo): alerts, timeline, resolutions.

Each incident's raw event goes through the production code path (`pipeline.normalize` with the seeded source,
`present.build_alert` with the seeded rule, `fingerprint.knowledge_key`), so a live alert for the same kind of
event gets the same knowledge key. Timestamps are relative to seed time; incidents that already exist are
left untouched (stable ids), so running the seed twice changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.core.audit import SYSTEM_ACTOR, user_actor
from app.core.ids import stable_id
from app.pipeline.envelope import format_ts
from app.pipeline.fingerprint import knowledge_key
from app.pipeline.normalize import normalize
from app.pipeline.present import build_alert
from app.pipeline.rules.index import rule_from_row
from app.pipeline.rules.templates import render
from seed.loaders._support import parse_offset, profile, replace_placeholders

log = logging.getLogger("seed")

# Sources fed through `POST /v1/alerts/raise`: their events arrive pre-normalized (`typed`).
TYPED_SOURCES = frozenset({"pr-review-agent"})
TIMELINE_TYPES = {
    "alert.created": "created",
    "notification.sent": "notified",
    "alert.escalated": "escalated",
    "alert.acknowledged": "acked",
    "comment": "comment",
    "action.run": "step_run",
    "alert.resolved": "resolved",
}


@dataclass
class Context:
    users: dict[str, asyncpg.Record]  # email -> id, display_name
    sources: dict[str, dict[str, Any]]
    team_domain: dict[UUID, UUID]


def typed_meta(source: dict[str, Any], raw: dict[str, Any], received_at: str) -> dict[str, Any]:
    """`_meta` as `POST /v1/alerts/raise` builds it (app/api/routers/ingest.py)."""
    attributes = raw.get("attributes") or {}
    return {
        "source": source["name"],
        "source_name": source["name"],
        "schema": raw["type"],
        "severity": raw["severity"],
        "service": attributes.get("service"),
        "environment": attributes.get("environment") or attributes.get("env"),
        "type": raw["type"],
        "title": raw["title"],
        "timestamp": received_at,
        "error_code": attributes.get("error_code") or attributes.get("errorCode"),
    }


def build_envelope(incident: dict[str, Any], source: dict[str, Any], opened: datetime) -> dict[str, Any]:
    opened_iso = format_ts(opened)
    raw = replace_placeholders(incident["raw"], opened_iso, int(opened.timestamp() * 1000))
    envelope: dict[str, Any] = {
        "event_id": str(stable_id("event", incident["id"])),
        "source_id": str(source["id"]),
        "received_at": opened_iso,
        "raw": raw,
    }
    if source["name"] in TYPED_SOURCES:
        envelope["typed"] = True
        envelope["_meta"] = typed_meta(source, raw, opened_iso)
    return envelope


async def _load_context(conn: asyncpg.Connection) -> Context:
    users = {r["email"]: r for r in await conn.fetch("SELECT id, email, display_name FROM users")}
    sources: dict[str, dict[str, Any]] = {}
    for row in await conn.fetch(
        "SELECT id, name, team_id, domain_id, type, preset, mapping, mapping_status, default_severity "
        "FROM sources"
    ):
        source = dict(row)
        status = source.pop("mapping_status")
        if isinstance(source["mapping"], dict) and status:
            source["mapping"] = {**source["mapping"], "status": status}
        sources[source["name"]] = source
    team_domain = {
        r["team_id"]: r["domain_id"]
        for r in await conn.fetch(
            "SELECT DISTINCT ON (team_id) team_id, domain_id FROM team_domains ORDER BY team_id, domain_id"
        )
    }
    return Context(users, sources, team_domain)


async def _rule_row(
    conn: asyncpg.Connection, incident: dict[str, Any], team_id: UUID
) -> asyncpg.Record | None:
    row = await conn.fetchrow(
        "SELECT id, team_id, enabled, version, definition FROM rules WHERE id = $1",
        stable_id("rule", incident["ruleKey"]),
    )
    if row is None:
        row = await conn.fetchrow(
            "SELECT id, team_id, enabled, version, definition FROM rules WHERE team_id = $1 AND name = $2",
            team_id,
            incident.get("ruleName"),
        )
    return row


def _actor(ctx: Context, who: str) -> dict[str, Any]:
    user = ctx.users.get(who)
    return user_actor(user["id"], user["display_name"]) if user else SYSTEM_ACTOR


def _timeline_text(entry: dict[str, Any], ctx: Context) -> str:
    name = ctx.users[entry["actor"]]["display_name"] if entry["actor"] in ctx.users else None
    if entry["type"] == "alert.acknowledged" and name:
        return f"Acknowledged by {name}"
    if entry["type"] == "alert.resolved" and name:
        return f"Resolved by {name}"
    return entry["text"]


async def _insert_incident(
    conn: asyncpg.Connection, ctx: Context, incident: dict[str, Any], now: datetime
) -> bool:
    key = incident["id"]
    source = ctx.sources.get(incident["source"])
    team_id = stable_id("team", incident["team"])
    if source is None:
        log.warning("history %s: source %r is not seeded; skipped", key, incident["source"])
        return False
    opened = now + parse_offset(incident["openedAt"])
    envelope = build_envelope(incident, source, opened)
    ev = normalize(envelope, source)

    domain_id = source["domain_id"] if source["team_id"] == team_id else ctx.team_domain[team_id]
    rule_row = await _rule_row(conn, incident, team_id)
    rule = rule_from_row(rule_row) if rule_row else None
    if rule is None:
        log.warning("history %s: rule %r not found; alert has no rule", key, incident["ruleKey"])
    rendered, fingerprint = build_alert(
        rule or {}, ev, envelope, render=render, domain_id=domain_id, now=opened.timestamp()
    )
    k_key = knowledge_key(
        domain_id,
        ev.meta.get("type") or ev.meta.get("schema"),
        ev.meta.get("service"),
        ev.meta.get("error_code"),
    )

    acked = opened + parse_offset(incident["ackAfter"]) if incident.get("ackAfter") else None
    resolved = opened + parse_offset(incident["resolveAfter"])
    ack_entry = next((e for e in incident["timeline"] if e["type"] == "alert.acknowledged"), None)
    acked_by = ctx.users[ack_entry["actor"]]["id"] if ack_entry else None
    resolver = ctx.users[incident["resolvedBy"]]["id"]
    escalations = sum(1 for e in incident["timeline"] if e["type"] == "alert.escalated")
    alert_id = stable_id("alert", key)

    await conn.execute(
        "INSERT INTO alerts (id, team_id, domain_id, rule_id, source_id, fingerprint, knowledge_key, "
        "title, body, severity, color, status, count, first_seen_at, last_seen_at, first_event_received_at, "
        "escalation_level, acked_at, acked_by, resolved_at, assignee_id, flags, fields, raw_event, "
        "runbook_id, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'resolved', $12, $13, $14, $13, "
        "$15, $16, $17, $18, $19, $20, $21, $22, $23, $13)",
        alert_id,
        team_id,
        domain_id,
        rule_row["id"] if rule_row else None,
        source["id"],
        fingerprint,
        k_key,
        incident.get("title") or rendered.title,
        rendered.body,
        incident["severity"],
        rendered.color,
        incident["count"],
        opened,
        max(opened, resolved - (resolved - opened) / 2),
        1 + escalations,
        acked,
        acked_by,
        resolved,
        acked_by or resolver,
        rendered.flags,
        rendered.fields,
        envelope["raw"],
        UUID(str(rule.runbook_id)) if rule is not None and rule.runbook_id else None,
    )
    for i, entry in enumerate(incident["timeline"]):
        data = {"resolutionNote": incident["resolutionNote"]} if entry["type"] == "alert.resolved" else {}
        await conn.execute(
            "INSERT INTO timeline (id, alert_id, at, type, actor, text, data) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (id) DO NOTHING",
            stable_id("timeline", f"{key}:{i}"),
            alert_id,
            opened + parse_offset(entry["at"]),
            TIMELINE_TYPES[entry["type"]],
            _actor(ctx, entry["actor"]),
            _timeline_text(entry, ctx),
            data,
        )
    await conn.execute(
        "INSERT INTO resolutions (id, alert_id, domain_id, team_id, knowledge_key, note, resolved_by, "
        "created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (id) DO NOTHING",
        stable_id("resolution", key),
        alert_id,
        domain_id,
        team_id,
        k_key,
        incident["resolutionNote"],
        resolver,
        resolved,
    )
    await _index_resolution_note(conn, key, domain_id, team_id, incident, resolved)
    return True


async def _index_resolution_note(
    conn: asyncpg.Connection, key: str, domain_id: UUID, team_id: UUID, incident: dict[str, Any], at: datetime
) -> None:
    """The note becomes a `resolution_note` knowledge doc with one chunk so similar-incident search finds it.
    The embedding is filled by `95_knowledge` (or later by the AI backfill)."""
    doc_id = stable_id("knowledge_doc", f"resolution:{key}")
    await conn.execute(
        "INSERT INTO knowledge_docs (id, domain_id, team_id, kind, title, created_at) "
        "VALUES ($1, $2, $3, 'resolution_note', $4, $5) ON CONFLICT (id) DO NOTHING",
        doc_id,
        domain_id,
        team_id,
        incident["title"],
        at,
    )
    await conn.execute(
        "INSERT INTO knowledge_chunks (id, doc_id, domain_id, team_id, text) VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (id) DO NOTHING",
        stable_id("knowledge_chunk", f"resolution:{key}:0"),
        doc_id,
        domain_id,
        team_id,
        f"{incident['title']}\n{incident['resolutionNote']}",
    )


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    if profile() == "core":
        return
    incidents = fixtures["history"]["incidents"]
    ids = [stable_id("alert", i["id"]) for i in incidents]
    existing = {r["id"] for r in await conn.fetch("SELECT id FROM alerts WHERE id = ANY($1)", ids)}
    todo = [i for i, alert_id in zip(incidents, ids, strict=True) if alert_id not in existing]
    if not todo:
        return
    ctx = await _load_context(conn)
    now = datetime.now(UTC)
    # Oldest first, so short ids grow with time.
    for incident in sorted(todo, key=lambda i: parse_offset(i["openedAt"])):
        await _insert_incident(conn, ctx, incident, now)
