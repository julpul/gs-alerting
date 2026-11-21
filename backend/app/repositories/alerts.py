"""Alert reads: list with cursor pagination, detail, id resolution. Every query is scope-filtered."""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
import orjson
from pydantic import ValidationError

from app.core.errors import AppError
from app.core.ids import format_short_id, parse_short_id, parse_uuid
from app.core.scope import Scope
from app.schemas import api as wire

log = logging.getLogger(__name__)

FLAGS = {f.value for f in wire.Flag}
ACTOR_KINDS = {"user", "agent", "system"}
SUGGESTION_KEYS = (
    "diagnosis",
    "nextStep",
    "nextStepId",
    "aiUnavailable",
    "confidence",
    "fixedBefore",
    "similarIncidents",
    "sources",
    "draftPullRequestUrl",
)
MAX_LIMIT = 200

_JOINS = """
    FROM alerts a
    LEFT JOIN teams t ON t.id = a.team_id
    LEFT JOIN sources s ON s.id = a.source_id
    LEFT JOIN rules r ON r.id = a.rule_id
    LEFT JOIN users u ON u.id = a.assignee_id
"""
_NAMES = "t.name AS team_name, s.name AS source_name, r.name AS rule_name, u.display_name AS assignee_name"
SUMMARY_SELECT = (
    "SELECT a.id, a.short_id, a.team_id, a.domain_id, a.rule_id, a.source_id, a.title, a.severity, "
    "a.color, a.status, a.count, a.first_seen_at, a.last_seen_at, a.escalation_level, a.ack_deadline, "
    f"a.flags, a.assignee_id, {_NAMES} {_JOINS}"
)
DETAIL_SELECT = f"SELECT a.*, {_NAMES} {_JOINS}"


def actor_out(stored: dict[str, Any] | None) -> dict[str, Any]:
    """Stored actor `{type, id, name, onBehalfOf}` -> wire Actor `{kind, id, displayName, onBehalfOf}`."""
    stored = stored or {}
    kind = stored.get("type") or stored.get("kind")
    return {
        "kind": kind if kind in ACTOR_KINDS else "system",
        "id": str(stored.get("id") or "system"),
        "displayName": stored.get("name") or stored.get("displayName") or "system",
        "onBehalfOf": stored.get("onBehalfOf"),
    }


def summary_out(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    assignee = None
    if row["assignee_id"]:
        assignee = {
            "kind": "user",
            "id": str(row["assignee_id"]),
            "displayName": row["assignee_name"] or "unknown",
            "onBehalfOf": None,
        }
    return {
        "id": str(row["id"]),
        "shortId": format_short_id(row["short_id"]),
        "title": row["title"],
        "severity": row["severity"],
        "color": row["color"],
        "status": row["status"],
        "count": row["count"],
        "firstSeenAt": row["first_seen_at"],
        "lastSeenAt": row["last_seen_at"],
        "teamId": str(row["team_id"]),
        "teamName": row["team_name"],
        "domainId": str(row["domain_id"]),
        "sourceName": row["source_name"],
        "ruleId": str(row["rule_id"]) if row["rule_id"] else None,
        "ruleName": row["rule_name"],
        "assignee": assignee,
        "escalationLevel": row["escalation_level"],
        "ackDeadline": row["ack_deadline"],
        "escalatesAt": row["ack_deadline"],
        "flags": [f for f in (row["flags"] or []) if f in FLAGS],
    }


def encode_cursor(last_seen_at: datetime, alert_id: UUID) -> str:
    raw = orjson.dumps([last_seen_at.isoformat(), str(alert_id)])
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        ts, ident = orjson.loads(raw)
        return datetime.fromisoformat(ts), UUID(ident)
    except (ValueError, TypeError, orjson.JSONDecodeError) as exc:
        raise AppError(422, "validation_error", "cursor: invalid cursor") from exc


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_alerts(
    conn: asyncpg.Connection,
    scope: Scope,
    *,
    statuses: list[str] | None = None,
    severities: list[str] | None = None,
    team_id: UUID | None = None,
    domain_id: UUID | None = None,
    source_id: UUID | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], str | None]:
    limit = max(1, min(limit, MAX_LIMIT))
    where: list[str] = []
    args: list[Any] = []

    def add(clause: str, value: Any) -> None:
        args.append(value)
        where.append(clause.format(n=len(args)))

    if not scope.is_master:
        add("a.team_id = ANY(${n})", list(scope.team_ids))
    if statuses:
        add("a.status = ANY(${n})", statuses)
    else:
        where.append("a.status <> 'resolved'")
    if severities:
        add("a.severity = ANY(${n})", severities)
    if team_id:
        add("a.team_id = ${n}", team_id)
    if domain_id:
        add("a.domain_id = ${n}", domain_id)
    if source_id:
        add("a.source_id = ${n}", source_id)
    if q and q.strip():
        add("a.title ILIKE '%' || ${n} || '%'", _escape_like(q.strip()))
    if cursor:
        ts, ident = decode_cursor(cursor)
        args.extend([ts, ident])
        where.append(f"(a.last_seen_at, a.id) < (${len(args) - 1}, ${len(args)})")

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = await conn.fetch(
        f"{SUMMARY_SELECT} {clause} ORDER BY a.last_seen_at DESC, a.id DESC LIMIT {limit + 1}", *args
    )
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1]["last_seen_at"], page[-1]["id"]) if len(rows) > limit else None
    return [summary_out(r) for r in page], next_cursor


async def find_alert(
    conn: asyncpg.Connection, ref: str, scope: Scope, *, for_update: bool = False, detail: bool = False
) -> asyncpg.Record | None:
    """Resolve `A-1234` or a uuid to an alert row visible in `scope` (None = unknown or not visible)."""
    short = parse_short_id(ref)
    ident = None if short is not None else parse_uuid(ref)
    if short is None and ident is None:
        return None
    where = "a.short_id = $1" if short is not None else "a.id = $1"
    args: list[Any] = [short if short is not None else ident]
    if not scope.is_master:
        where += " AND a.team_id = ANY($2)"
        args.append(list(scope.team_ids))
    base = DETAIL_SELECT if detail else SUMMARY_SELECT
    lock = " FOR UPDATE OF a" if for_update else ""
    return await conn.fetchrow(f"{base} WHERE {where}{lock}", *args)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    return orjson.dumps(value).decode()


def _field_pairs(fields: Any) -> list[dict[str, str]]:
    if isinstance(fields, dict):
        return [{"label": str(k), "value": _stringify(v)} for k, v in fields.items()]
    if isinstance(fields, list):
        return [
            {"label": str(f.get("label", "")), "value": _stringify(f.get("value"))}
            for f in fields
            if isinstance(f, dict)
        ]
    return []


async def _deliveries(conn: asyncpg.Connection, alert_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT d.id, d.alert_id, d.recipient_user_id, d.recipient_address, d.channel, d.kind, d.status, "
        "d.attempts, COALESCE(d.delivered_at, d.sent_at, d.created_at) AS updated_at, "
        "u.display_name FROM deliveries d LEFT JOIN users u ON u.id = d.recipient_user_id "
        "WHERE d.alert_id = $1 ORDER BY d.created_at, d.id",
        alert_id,
    )
    return [
        {
            "id": str(r["id"]),
            "alertId": str(r["alert_id"]),
            "recipient": {
                "kind": "user" if r["recipient_user_id"] else "system",
                "id": str(r["recipient_user_id"] or r["recipient_address"] or "unknown"),
                "displayName": r["display_name"] or r["recipient_address"] or "unknown",
                "onBehalfOf": None,
            },
            "channel": r["channel"],
            "kind": r["kind"],
            "status": r["status"],
            "attempts": r["attempts"],
            "updatedAt": r["updated_at"],
        }
        for r in rows
    ]


def timeline_out(r: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "at": r["at"],
        "type": r["type"],
        "actor": actor_out(r["actor"]),
        "text": r["text"],
        "data": r["data"] or {},
    }


async def _timeline(conn: asyncpg.Connection, alert_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, at, type, actor, text, data FROM timeline WHERE alert_id = $1 ORDER BY at, id", alert_id
    )
    return [timeline_out(r) for r in rows]


async def _suggestion(conn: asyncpg.Connection, alert_id: UUID) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT id, alert_id, status, content FROM suggestions WHERE alert_id = $1 "
        "AND status NOT IN ('failed', 'error', 'pending') ORDER BY created_at DESC LIMIT 1",
        alert_id,
    )
    if row is None:
        return None
    content = row["content"] or {}
    data = {k: content[k] for k in SUGGESTION_KEYS if k in content}
    data.update(
        id=str(row["id"]),
        alertId=str(row["alert_id"]),
        status=row["status"] if row["status"] in ("accepted", "rejected") else "draft",
    )
    try:
        return wire.Suggestion.model_validate(data).model_dump(mode="json")
    except ValidationError:
        log.warning("suggestion %s has unexpected content shape", row["id"])
        return None


async def _runbook(conn: asyncpg.Connection, alert: asyncpg.Record) -> dict[str, Any] | None:
    try:
        from app.runbooks.lookup import runbook_for_alert
    except ImportError:
        return None
    try:
        view = await runbook_for_alert(conn, dict(alert))
        if view is None:
            return None
        data = view.model_dump(mode="json") if hasattr(view, "model_dump") else dict(view)
        return wire.Runbook.model_validate(data).model_dump(mode="json")
    except Exception:
        log.exception("runbook lookup failed for alert %s", alert["id"])
        return None


async def alert_detail(conn: asyncpg.Connection, row: asyncpg.Record) -> dict[str, Any]:
    """`row` must come from `find_alert(..., detail=True)`."""
    out = summary_out(row)
    out.update(
        body=row["body"],
        fields=_field_pairs(row["fields"]),
        rawEvent=row["raw_event"],
        groupedItems=row["grouped_items"] or [],
        runbook=await _runbook(conn, row),
        suggestion=await _suggestion(conn, row["id"]),
        deliveries=await _deliveries(conn, row["id"]),
        timeline=await _timeline(conn, row["id"]),
        ackedAt=row["acked_at"],
        resolvedAt=row["resolved_at"],
        nextRenotifyAt=row["next_renotify_at"],
    )
    return out
