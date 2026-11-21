"""Resolution note draft from the alert timeline (spec 12). Always a draft; the user edits and saves it."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from pydantic import BaseModel

from app.ai import prompts
from app.ai.llm import AIUnavailable, complete_for_domain

log = logging.getLogger(__name__)

NOTE_TYPES = (
    "comment",
    "step_run",
    "acked",
    "assigned",
    "escalated",
    "approval_requested",
    "approval_decided",
)
MAX_ENTRIES = 40
MAX_NOTE = 1500


class NoteAnswer(BaseModel):
    note: str


def _actor_name(actor: Any) -> str:
    return str(actor.get("name") or "system") if isinstance(actor, dict) else "system"


def fallback_note(entries: list[dict[str, str]]) -> str:
    """Deterministic summary used when the model is unavailable."""
    useful = [e for e in entries if e["type"] in ("comment", "step_run")]
    if not useful:
        return ""
    return "Actions taken: " + "; ".join(f"{e['text']} ({e['actor']})" for e in useful)[:MAX_NOTE]


async def draft_note(conn: asyncpg.Connection, alert: asyncpg.Record) -> dict[str, Any]:
    rows = await conn.fetch(
        "SELECT at, type, actor, text FROM timeline WHERE alert_id = $1 AND type = ANY($2) "
        "ORDER BY at LIMIT $3",
        alert["id"],
        list(NOTE_TYPES),
        MAX_ENTRIES,
    )
    entries = [
        {
            "at": r["at"].isoformat(timespec="seconds"),
            "type": r["type"],
            "actor": _actor_name(r["actor"]),
            "text": r["text"],
        }
        for r in rows
    ]
    system, user = prompts.note_prompt(
        {"title": alert["title"], "severity": alert["severity"], "body": alert["body"]}, entries
    )
    try:
        result = await complete_for_domain(
            conn, alert["domain_id"], system=system, user=user, schema=NoteAnswer, tier="smart"
        )
    except AIUnavailable as exc:
        log.info("resolution note without AI: %s", exc)
        return {"note": fallback_note(entries), "aiUnavailable": True, "model": None}
    return {"note": result.parsed.note.strip()[:MAX_NOTE], "aiUnavailable": False, "model": result.model}
