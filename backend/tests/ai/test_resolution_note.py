"""Deterministic resolution-note draft (no LLM) and the ai/heuristic labelling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.ai.resolution_note import draft_note, fallback_note
from tests.api.alerts_helpers import make_alert

T0 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


def entry(kind: str, text: str, actor: str = "Anna", minutes: int = 0) -> dict:
    when = T0 + timedelta(minutes=minutes)
    return {"at": when.isoformat(), "when": when, "type": kind, "actor": actor, "text": text}


def test_fallback_note_summarises_times_steps_approvals_and_comments():
    entries = [
        entry("created", "Alert created", "system"),
        entry("acked", "Acknowledged by Anna", minutes=4),
        entry("step_run", "Ran retry_workflow: succeeded", minutes=6),
        entry("step_run", "Ran retry_workflow: succeeded", minutes=6),  # duplicates collapse
        entry("approval", "Approval requested for escalate (high risk) by Anna", minutes=7),
        entry("comment", "Counterparty confirmed the fix", minutes=9),
        entry("resolved", "Resolved by Piotr", "Piotr", minutes=95),
    ]
    alert = {"title": "Settlement timeout", "severity": "error", "first_seen_at": T0}
    note = fallback_note(entries, alert)
    assert note.splitlines() == [
        "Settlement timeout (error), opened 2026-11-21 09:00 UTC.",
        "Acknowledged by Anna after 4 min.",
        "Resolved by Piotr after 95 min.",
        "Actions taken: Ran retry_workflow: succeeded (Anna).",
        "Approvals: Approval requested for escalate (high risk) by Anna (Anna).",
        "Comments: Counterparty confirmed the fix (Anna).",
    ]
    assert "after 30 s" in fallback_note(
        [entry("acked", "a")], {"first_seen_at": T0 - timedelta(seconds=30)}
    )
    assert "after 3 h 5 min" in fallback_note([entry("resolved", "r", minutes=185)], {"first_seen_at": T0})


async def test_empty_model_answer_falls_back_to_the_heuristic_draft(seeded, llm):
    alert = await make_alert(seeded, title="Empty answer")
    await seeded.execute(
        "INSERT INTO timeline (id, alert_id, type, actor, text) VALUES ($1, $2, 'comment', $3, 'restarted')",
        uuid4(),
        alert["id"],
        {"type": "user", "id": "x", "name": "Anna"},
    )
    llm.responses = {"smart": {"note": "   "}}
    async with seeded.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM alerts WHERE id = $1", alert["id"])
        draft = await draft_note(conn, row)
    assert draft["source"] == "heuristic" and draft["aiUnavailable"] is True
    assert "Comments: restarted (Anna)." in draft["note"]
