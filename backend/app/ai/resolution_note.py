"""Resolution note draft from the alert timeline (spec 12). Always a draft; the user edits and saves it.

With a model the draft is AI-written (`source: "ai"`). Without one (or when it answers nothing) the draft
is a deterministic summary of the timeline (`source: "heuristic"`, `aiUnavailable: true`): when it opened,
who acknowledged / resolved it and how long that took, the runbook steps run, approvals and comments.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import asyncpg
from pydantic import BaseModel

from app.ai import prompts
from app.ai.llm import AIUnavailable, complete_for_domain

log = logging.getLogger(__name__)

NOTE_TYPES = (
    "created",
    "comment",
    "step_run",
    "acked",
    "assigned",
    "escalated",
    "approval",
    "approval_requested",
    "approval_decided",
    "resolved",
)
MAX_ENTRIES = 40
MAX_NOTE = 1500
MAX_ITEMS = 8


class NoteAnswer(BaseModel):
    note: str


def _actor_name(actor: Any) -> str:
    return str(actor.get("name") or "system") if isinstance(actor, dict) else "system"


def _duration(start: datetime | None, end: datetime | None) -> str | None:
    if start is None or end is None:
        return None
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def _stamp(at: datetime | None) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC") if at else "?"


def _first(entries: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((e for e in entries if e["type"] == kind), None)


def _items(entries: list[dict[str, Any]], kinds: tuple[str, ...]) -> list[str]:
    found = [f"{e['text']} ({e['actor']})" for e in entries if e["type"] in kinds and e["text"]]
    return list(dict.fromkeys(found))[:MAX_ITEMS]


def fallback_note(entries: list[dict[str, Any]], alert: dict[str, Any] | None = None) -> str:
    """Deterministic draft from the timeline: times, steps run, approvals, comments."""
    alert = alert or {}
    opened = _first(entries, "created")
    acked = _first(entries, "acked")
    resolved = _first(entries, "resolved")
    start = alert.get("first_seen_at") or (opened or {}).get("when")
    acked_at = alert.get("acked_at") or (acked or {}).get("when")
    resolved_at = alert.get("resolved_at") or (resolved or {}).get("when")
    lines: list[str] = []
    if alert.get("title"):
        lines.append(f"{alert['title']} ({alert.get('severity') or 'alert'}), opened {_stamp(start)}.")
    if acked:
        took = _duration(start, acked_at)
        lines.append(f"Acknowledged by {acked['actor']}" + (f" after {took}." if took else "."))
    if resolved:
        took = _duration(start, resolved_at)
        lines.append(f"Resolved by {resolved['actor']}" + (f" after {took}." if took else "."))
    sections = (
        ("Actions taken", ("step_run",)),
        ("Approvals", ("approval", "approval_requested", "approval_decided")),
        ("Escalations", ("escalated",)),
        ("Comments", ("comment",)),
    )
    for label, kinds in sections:
        items = _items(entries, kinds)
        if items:
            lines.append(f"{label}: " + "; ".join(items) + ".")
    if not any(_items(entries, kinds) for _, kinds in sections):
        lines.append("No actions were recorded on the alert; describe the fix here.")
    return "\n".join(lines)[:MAX_NOTE]


async def timeline_entries(conn: asyncpg.Connection, alert_id: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT at, type, actor, text FROM timeline WHERE alert_id = $1 AND type = ANY($2) "
        "ORDER BY at LIMIT $3",
        alert_id,
        list(NOTE_TYPES),
        MAX_ENTRIES,
    )
    return [
        {
            "at": r["at"].isoformat(timespec="seconds"),
            "when": r["at"],
            "type": r["type"],
            "actor": _actor_name(r["actor"]),
            "text": r["text"],
        }
        for r in rows
    ]


def _heuristic(entries: list[dict[str, Any]], alert: dict[str, Any]) -> dict[str, Any]:
    note = fallback_note(entries, alert)
    return {"note": note, "aiUnavailable": True, "model": None, "source": "heuristic"}


async def draft_note(conn: asyncpg.Connection, alert: asyncpg.Record) -> dict[str, Any]:
    entries = await timeline_entries(conn, alert["id"])
    facts = dict(alert)
    system, user = prompts.note_prompt(
        {"title": alert["title"], "severity": alert["severity"], "body": alert["body"]},
        [{k: v for k, v in e.items() if k != "when"} for e in entries],
    )
    try:
        result = await complete_for_domain(
            conn, alert["domain_id"], system=system, user=user, schema=NoteAnswer, tier="smart"
        )
    except AIUnavailable as exc:
        log.info("resolution note without AI: %s", exc)
        return _heuristic(entries, facts)
    note = result.parsed.note.strip()[:MAX_NOTE]
    if not note:
        return _heuristic(entries, facts)
    return {"note": note, "aiUnavailable": False, "model": result.model, "source": "ai"}
