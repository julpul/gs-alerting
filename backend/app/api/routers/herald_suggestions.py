"""Herald UI: `GET /api/alerts/{id}/suggestion`, the AI card of the alert drawer.

Unlike `GET /v1/alerts/{id}/suggestion` (404 until the ai worker has written one) this says what the
drawer should show: `ready` (with the suggestion and the model that answered), `pending` (the worker will
write one shortly: show a loading state and poll) or `none` (no suggestion is coming: resolved, too old, or
a severity the worker skips). Reads only; the suggestion is still produced by the ai worker, never here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.ai.retrieval import enrich_history
from app.ai.suggest import SUGGEST_SEVERITIES
from app.api.deps import Pool
from app.api.routers.herald_alerts import _find
from app.api.routers.suggestions import VISIBLE, suggestion_out
from app.herald.identity import Actor
from app.herald.routing import herald_router

router = herald_router(prefix="/api/alerts", tags=["herald"])

PENDING_FOR = timedelta(minutes=5)
SUSPICIOUS = "suspicious_content"


def model_name(model: str | None) -> str | None:
    """`anthropic/claude-haiku-5-5` -> `claude-haiku-5-5`; None when no model answered (degraded)."""
    name = (model or "").strip().removesuffix(" (reused)").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return name or None


@router.get("/{alert_id}/suggestion")
async def get_suggestion(alert_id: str, who: Actor, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        alert = await _find(conn, alert_id, who)
        facts = await conn.fetchrow(
            "SELECT status, severity, flags, created_at, EXISTS (SELECT 1 FROM deliveries d "
            "WHERE d.alert_id = a.id AND d.kind = 'hard') AS paged FROM alerts a WHERE a.id = $1",
            alert["id"],
        )
        suspicious = SUSPICIOUS in (facts["flags"] or [])
        row = await conn.fetchrow(
            "SELECT id, alert_id, status, content, model FROM suggestions WHERE alert_id = $1 "
            "AND status = ANY($2) ORDER BY created_at DESC LIMIT 1",
            alert["id"],
            list(VISIBLE),
        )
        if row is not None:
            content = await enrich_history(conn, row["content"] or {})
            return {
                "state": "ready",
                "suggestion": suggestion_out(row, content),
                "model": None if content.get("aiUnavailable") else model_name(row["model"]),
                "suspicious": suspicious,
            }
    eligible = facts["severity"] in SUGGEST_SEVERITIES or facts["paged"]
    fresh = datetime.now(UTC) - facts["created_at"] < PENDING_FOR
    pending = facts["status"] != "resolved" and eligible and fresh
    return {
        "state": "pending" if pending else "none",
        "suggestion": None,
        "model": None,
        "suspicious": suspicious,
    }
