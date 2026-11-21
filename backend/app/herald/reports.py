"""Status update and postmortem drafts (Herald `incident.py`), built from a Herald-shaped `AlertDetail`.

Deterministic text from what the alert records; judgement (impact, causes) is left as prompts. The
postmortem may add an AI summary drafted from the timeline (`app.ai.resolution_note`) when available.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.herald.views import SEVERITY_GUIDE

_DONE = ("done", "failed", "rejected", "awaiting_approval")
_ACTION_WORDS = {
    "ack": "acknowledged the alert",
    "resolve": "resolved the alert",
    "step_done": "completed a manual step",
    "action_run": "ran an action",
    "approval_requested": "asked for approval",
    "approved": "approved the request",
    "rejected": "rejected the request",
    "note": "noted",
    "assigned": "handed the alert over",
}


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _clock(value: Any) -> str:
    moment = _time(value)
    return moment.strftime("%H:%M UTC") if moment else "unknown"


def _minutes(start: Any, end: Any) -> str:
    a, b = _time(start), _time(end)
    if not a or not b:
        return "unknown"
    seconds = max((b - a).total_seconds(), 0)
    return f"{seconds / 60:.0f} min" if seconds >= 90 else f"{seconds:.0f} s"


def _steps(alert: dict[str, Any]) -> tuple[list[str], str | None]:
    state = alert.get("step_state") or {}
    done, upcoming = [], None
    for step in (alert.get("runbook") or {}).get("steps", []):
        current = (state.get(str(step["id"])) or {}).get("state")
        if current in _DONE:
            done.append(f"{step['text']} ({current.replace('_', ' ')})")
        elif upcoming is None:
            upcoming = step["text"]
    return done, upcoming


def status_update(alert: dict[str, Any], now: datetime, names: dict[str, str]) -> str:
    """What, since when, who is in charge, status, done so far, next step, next update."""
    guide = SEVERITY_GUIDE.get(alert["severity"], SEVERITY_GUIDE["info"])
    done, upcoming = _steps(alert)
    if alert["status"] == "resolved":
        status = f"Resolved at {_clock(alert.get('resolved_at'))}."
    elif alert["status"] == "acked":
        status = f"Being worked on since {_clock(alert.get('acked_at'))}."
    else:
        status = "Not yet acknowledged."
    every = guide["update_every_minutes"]
    if alert["status"] == "resolved":
        next_update = "No further updates: resolved."
    elif every:
        next_update = f"Next update by {_clock(now + timedelta(minutes=every))}."
    else:
        next_update = "Next update when something changes."
    manager = alert.get("incident_manager")
    lines = [
        f"{alert['key']} · {alert['severity'].upper()} · {alert['title']}",
        "",
        f"What is happening: {alert.get('body') or 'unknown'}",
        f"Since: {_clock(alert.get('first_seen'))} (seen {alert.get('count', 1)} time(s), "
        f"last at {_clock(alert.get('last_seen'))})",
        "Who is affected: unknown — fill in",
        f"In charge: {names.get(manager, manager) if manager else 'nobody yet'}",
        f"Current status: {status}",
        "Done so far:" if done else "Done so far: nothing yet",
        *[f"  - {item}" for item in done],
        f"Next step: {upcoming or 'unknown — fill in'}",
        next_update,
    ]
    return "\n".join(lines)


def _role(actor: str, alert: dict[str, Any], lead: str | None) -> str:
    if actor.startswith("system:"):
        return "Herald (automatic)"
    if actor.startswith("agent:"):
        return "an agent"
    if actor == alert.get("incident_manager"):
        return "the incident manager"
    if lead and actor == f"person:{lead}":
        return "the team lead"
    return "a responder"


def _resolver(ref: str) -> str:
    if not ref:
        return "nobody yet"
    return "Herald (automatic)" if ref.startswith("system:") else "a responder"


def postmortem(
    alert: dict[str, Any],
    *,
    lead: str | None,
    names: dict[str, str] | None,
    resolution_notes: list[str],
    ai_summary: str | None,
) -> str:
    """Blameless postmortem draft; roles instead of names unless `names` (ref -> display name) is given."""
    timeline = alert.get("timeline") or []
    done, _ = _steps(alert)
    rows = []
    for entry in timeline:
        who = names.get(entry["actor"], entry["actor"]) if names else _role(entry["actor"], alert, lead)
        what = _ACTION_WORDS.get(entry["action"], entry["action"].replace("_", " "))
        detail = entry.get("detail") or {}
        extra = detail.get("text") if entry["action"] in ("note",) else detail.get("action") or ""
        if not extra and entry["action"] not in _ACTION_WORDS:
            extra = detail.get("text") or ""
        text = f"{what}: {extra}" if extra else what
        rows.append(f"| {_clock(entry['at'])} | {who} | {text.replace('|', '/')} |")
    notes = [e for e in timeline if e["action"] == "note"]
    resolver = str(alert.get("resolved_by") or "")
    lines = [
        f"# Postmortem draft: {alert['key']} {alert['title']}",
        "",
        "> Draft generated from the alert timeline. Blameless: it describes what the system and the roles "
        "did, not who was at fault. Edit before sharing.",
        "",
        "## Summary",
        f"{alert['severity'].capitalize()} alert for {alert['owner']}: "
        f"{alert.get('body') or alert['title']}. Seen {alert.get('count', 1)} time(s).",
        *(["", f"AI draft from the timeline (check it): {ai_summary}"] if ai_summary else []),
        "",
        "## Impact",
        "_Who was affected, what did they see, how many, for how long? "
        "Any money, data or regulatory effect?_",
        "",
        "## Detection",
        f"- First seen: {_clock(alert.get('first_seen'))}",
        f"- Time to acknowledge: {_minutes(alert.get('first_seen'), alert.get('acked_at'))}",
        "- _Did we know before the people affected did? Could the rule have fired earlier?_",
        "",
        "## Response",
        "| Time | Who | What |",
        "|---|---|---|",
        *(rows or ["| | | _no recorded actions_ |"]),
        "",
        "## Recovery",
        f"- Resolved: {_clock(alert.get('resolved_at'))} by {_resolver(resolver)}",
        f"- Time to recover: {_minutes(alert.get('first_seen'), alert.get('resolved_at'))}",
        *[f"- Resolution note: {n}" for n in resolution_notes],
        "",
        "## Causes",
        "- _What directly triggered it?_",
        '- _What should change so this whole class of problem stops? (ask "why" until you reach it)_',
        "",
        "## Runbook",
        f"- Runbook used: {(alert.get('runbook') or {}).get('title') or 'none attached'}",
        *[f"- Step taken: {item}" for item in done],
        *[
            f"- Not in the runbook, noted during the response: {(e.get('detail') or {}).get('text', '')}"
            for e in notes
        ],
        "- _What should be added to, or removed from, the runbook?_",
        "",
        "## Follow-up actions",
        "| Action (start with a verb, be specific) | Owner | Due | Type (priority / improvement) |",
        "|---|---|---|---|",
        "| | | | |",
    ]
    return "\n".join(lines)
