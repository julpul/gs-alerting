"""Fix suggestion pipeline (spec 13.2): L1 exact memory, L2 hybrid retrieval, constrained LLM answer, save.

The LLM may only point at existing runbook step ids and cited ids it was shown; anything else is dropped.
Without an LLM the deterministic part (fixed-before, similar incidents, knowledge) is saved with
`aiUnavailable: true`. Alerts flagged `suspicious_content` never get an action suggestion.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel, Field

from app.ai import prompts
from app.ai.llm import AIUnavailable, complete_for_domain
from app.ai.redact import redact
from app.ai.retrieval import Hit, KnowledgeScope, exact_memory, hybrid_search, incident_ref, incident_refs
from app.core.ids import uuid7
from app.core.sse import publish_sse
from app.core.timeline import add_timeline
from app.pipeline.injection import FLAG as SUSPICIOUS

log = logging.getLogger(__name__)

SUGGEST_SEVERITIES = {"warning", "error", "critical"}
RESOLUTION_KIND = "resolution_note"
AI_ACTOR = {"type": "system", "id": None, "name": "AI assistant", "onBehalfOf": None}
MAX_TEXT = 600


class SuggestionAnswer(BaseModel):
    """What the model must return (spec 13.2.5)."""

    diagnosis: str = ""
    nextStepId: str | None = None
    nextStepText: str = ""
    confidence: float = Field(default=0.0)
    citedSourceIds: list[str] = Field(default_factory=list)


async def _runbook(
    conn: asyncpg.Connection, alert: asyncpg.Record
) -> tuple[str | None, list[dict[str, Any]]]:
    """(runbook name, steps) for the alert; empty when the runbooks module or a runbook is missing."""
    try:
        from app.runbooks.lookup import runbook_for_alert
    except ImportError:
        return None, []
    try:
        view = await runbook_for_alert(conn, dict(alert))
    except Exception:
        log.warning("runbook lookup failed for alert %s", alert["id"], exc_info=True)
        return None, []
    if view is None:
        return None, []
    data = view.model_dump(mode="json") if hasattr(view, "model_dump") else dict(view)
    steps = [s for s in data.get("steps") or [] if isinstance(s, dict) and s.get("id")]
    return data.get("name"), steps


def _should_suggest(alert: asyncpg.Record, has_hard: bool) -> bool:
    return alert["status"] != "resolved" and (alert["severity"] in SUGGEST_SEVERITIES or has_hard)


def _note_of(text: str) -> str:
    return text.split("Resolution:", 1)[-1].strip()


async def _similar(conn: asyncpg.Connection, hits: list[Hit]) -> tuple[list[dict[str, Any]], list[Hit]]:
    """Split hits into `similarIncidents` (resolution notes) and other knowledge hits."""
    incident_hits = [h for h in hits if h.kind == RESOLUTION_KIND and h.alert_id]
    notes = {
        r["alert_id"]: r["note"]
        for r in await conn.fetch(
            "SELECT DISTINCT ON (alert_id) alert_id, note FROM resolutions WHERE alert_id = ANY($1) "
            "ORDER BY alert_id, created_at DESC",
            [h.alert_id for h in incident_hits],
        )
    }
    refs = await incident_refs(conn, [h.alert_id for h in incident_hits if h.alert_id])
    incidents = [
        {
            "alertId": str(h.alert_id),
            **incident_ref(refs, h.alert_id, h.title),
            "resolvedAt": (refs.get(h.alert_id) or {}).get("resolvedAt"),
            "similarity": round(h.similarity, 3),
            "resolutionNote": notes.get(h.alert_id) or _note_of(h.text),
        }
        for h in incident_hits
    ]
    return incidents, [h for h in hits if h.kind != RESOLUTION_KIND]


def _sources(hits: list[Hit]) -> list[dict[str, Any]]:
    return [{"kind": "knowledge", "title": h.title, **({"url": h.url} if h.url else {})} for h in hits]


def validate_answer(
    answer: SuggestionAnswer,
    *,
    steps: dict[str, dict[str, Any]],
    citable: dict[str, Hit | None],
    allow_action: bool,
) -> dict[str, Any]:
    """Drop invented step ids and citations; clamp confidence. Returns the validated fields."""
    step = steps.get(answer.nextStepId) if allow_action and answer.nextStepId else None
    next_text = ""
    if step is not None:
        next_text = str(step.get("text") or answer.nextStepText)
    elif allow_action and not answer.nextStepId:
        next_text = answer.nextStepText
    cited = [c for c in dict.fromkeys(answer.citedSourceIds) if c in citable]
    return {
        "diagnosis": redact(answer.diagnosis).strip()[:MAX_TEXT],
        "nextStepId": step["id"] if step is not None else None,
        "nextStep": redact(next_text).strip()[:MAX_TEXT],
        "confidence": min(max(answer.confidence, 0.0), 1.0),
        "cited": cited,
    }


def _degraded_diagnosis(fixed_before: list[dict[str, Any]]) -> str:
    if fixed_before:
        first = fixed_before[0]
        where = f" ({first['shortId']} {first['title']})" if first.get("shortId") else ""
        return f"AI unavailable. Fixed before{where}: {first['resolutionNote']}"
    return "AI unavailable. Showing matching history and knowledge only."


async def build_content(conn: asyncpg.Connection, alert: asyncpg.Record) -> tuple[dict[str, Any], str | None]:
    """The suggestion content and the model that answered (None when degraded)."""
    flags = list(alert["flags"] or [])
    suspicious = SUSPICIOUS in flags
    fixed_before = await exact_memory(
        conn, alert["domain_id"], alert["knowledge_key"], exclude_alert=alert["id"]
    )
    query = f"{alert['title']} {(alert['body'] or '')[:400]}"
    hits = await hybrid_search(
        conn, query, KnowledgeScope.of_alert(alert["domain_id"], alert["team_id"]), exclude_alert=alert["id"]
    )
    incidents, knowledge = await _similar(conn, hits)
    runbook_name, steps = await _runbook(conn, alert)
    steps_by_id = {str(s["id"]): s for s in steps}

    content: dict[str, Any] = {
        "fixedBefore": fixed_before,
        "similarIncidents": incidents,
        "sources": [],
        "draftPullRequestUrl": None,
        "aiUnavailable": False,
    }
    citable: dict[str, Hit | None] = {}
    history: list[dict[str, Any]] = []
    for index, item in enumerate(fixed_before, start=1):
        citable[f"F{index}"] = None
        history.append({"id": f"F{index}", "kind": "fixed_before", "note": item["resolutionNote"][:MAX_TEXT]})
    for index, hit in enumerate(hits, start=1):
        citable[f"S{index}"] = hit
        history.append({"id": f"S{index}", "kind": hit.kind, "title": hit.title, "text": hit.text[:MAX_TEXT]})
    event = {
        "title": alert["title"],
        "body": alert["body"],
        "severity": alert["severity"],
        "fields": alert["fields"],
        "raw": alert["raw_event"],
    }
    step_view = [
        {
            "id": s["id"],
            "kind": s.get("kind"),
            "text": str(s.get("text") or "")[:300],
            "action": s.get("action"),
        }
        for s in steps
    ]
    system, user = prompts.suggestion_prompt(
        event=event, steps=step_view, history=history, allow_action=not suspicious
    )
    try:
        result = await complete_for_domain(
            conn, alert["domain_id"], system=system, user=user, schema=SuggestionAnswer, tier="fast"
        )
    except AIUnavailable as exc:
        log.info("AI unavailable for alert %s: %s", alert["id"], exc)
        content.update(
            aiUnavailable=True,
            diagnosis=_degraded_diagnosis(fixed_before),
            sources=_sources(knowledge),
        )
        return content, None

    checked = validate_answer(result.parsed, steps=steps_by_id, citable=citable, allow_action=not suspicious)
    cited_hits = [citable[c] for c in checked["cited"] if citable[c] is not None]
    sources = _sources([h for h in cited_hits if h.kind != RESOLUTION_KIND])
    if checked["nextStepId"] and runbook_name:
        sources.append({"kind": "runbook", "title": runbook_name})
    content.update(diagnosis=checked["diagnosis"], confidence=checked["confidence"], sources=sources)
    if checked["nextStep"]:
        content["nextStep"] = checked["nextStep"]
    if checked["nextStepId"]:
        content["nextStepId"] = checked["nextStepId"]
    return content, result.model


STORM_REUSE_SECONDS = 120


async def _recent_sibling_suggestion(
    conn: asyncpg.Connection, alert: asyncpg.Record
) -> tuple[dict[str, Any], str] | None:
    """Storm/cost guard: during an incident many new alerts share one rule and knowledge key. The
    first gets a full suggestion (embeddings + LLM); siblings within STORM_REUSE_SECONDS reuse it
    instead of paying for another model call. Same rule ⇒ same runbook, so step ids stay valid."""
    if not alert["knowledge_key"] or alert["rule_id"] is None:
        return None
    row = await conn.fetchrow(
        # driven by the small recent-suggestions window (index suggestions_created), not by alerts
        "SELECT s.content, s.model, a.short_id FROM suggestions s JOIN alerts a ON a.id = s.alert_id "
        "WHERE s.created_at > now() - make_interval(secs => $4) "
        "AND s.status IN ('ready', 'degraded') "
        "AND a.rule_id = $1 AND a.knowledge_key = $2 AND a.id <> $3 "
        "ORDER BY s.created_at DESC LIMIT 1",
        alert["rule_id"],
        alert["knowledge_key"],
        alert["id"],
        float(STORM_REUSE_SECONDS),
    )
    if row is None:
        return None
    content = dict(row["content"])
    content.setdefault("reusedFrom", f"A-{row['short_id']}")  # always point at the original
    model = row["model"] or "none"
    return content, model if model.endswith("(reused)") else f"{model} (reused)"


async def generate_suggestion(conn: asyncpg.Connection, alert_id: UUID) -> dict[str, Any] | None:
    """Create the suggestion of one alert; None when skipped (unknown alert, gate, or one exists already)."""
    alert = await conn.fetchrow("SELECT * FROM alerts WHERE id = $1", alert_id)
    if alert is None:
        return None
    if await conn.fetchval("SELECT 1 FROM suggestions WHERE alert_id = $1 LIMIT 1", alert_id):
        return None
    has_hard = bool(
        await conn.fetchval(
            "SELECT 1 FROM deliveries WHERE alert_id = $1 AND kind = 'hard' LIMIT 1", alert_id
        )
    )
    if not _should_suggest(alert, has_hard):
        return None

    started = time.perf_counter()
    reused = await _recent_sibling_suggestion(conn, alert)
    if reused is not None:
        content, model = reused
    else:
        content, model = await build_content(conn, alert)
    latency_ms = int((time.perf_counter() - started) * 1000)
    status = "degraded" if content["aiUnavailable"] else "ready"
    suggestion_id = uuid7()
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"suggestion:{alert_id}")
        if await conn.fetchval("SELECT 1 FROM suggestions WHERE alert_id = $1 LIMIT 1", alert_id):
            return None
        await conn.execute(
            "INSERT INTO suggestions (id, alert_id, status, content, model, latency_ms) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            suggestion_id,
            alert_id,
            status,
            content,
            model,
            latency_ms,
        )
        text = (
            "AI suggestion ready (AI unavailable: history only)"
            if content["aiUnavailable"]
            else "AI suggestion ready"
        )
        await add_timeline(
            conn,
            alert_id,
            "suggestion",
            text,
            actor=AI_ACTOR,
            data={"suggestionId": str(suggestion_id), "model": model},
        )
    try:
        await publish_sse(
            "suggestion.ready",
            {"alertId": str(alert_id), "suggestionId": str(suggestion_id), "status": status},
            team_ids=[alert["team_id"]],
        )
    except Exception:
        log.warning("could not publish suggestion.ready", exc_info=True)
    return {
        "id": suggestion_id,
        "status": status,
        "content": content,
        "model": model,
        "latency_ms": latency_ms,
    }
