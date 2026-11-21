"""Our runbooks / runbook steps in Herald's `Runbook` and `Step` shapes.

Steps use HB-CORE's `views.step_out` (id = 1-based `order`, our step id = `uid`, `text` -> `manual`,
`guard` -> `condition`); a step whose action mapping was guessed (`suggested`) also shows it as `suggest`.
"""

from __future__ import annotations

from typing import Any

from app.herald.views import step_out
from app.schemas import api as wire

PARSED_BY = {"ai": "llm", "llm": "llm", "human": "human"}


def step_wire(step: dict[str, Any], position: int) -> dict[str, Any]:
    """HB-CORE's `views.step_out` + Herald's `suggest` for an action the parser guessed."""
    parsed = wire.RunbookStep.model_validate(step)
    out = step_out(parsed)
    out["id"] = parsed.order or position
    if parsed.suggested and parsed.action:
        out["suggest"] = parsed.action
    return out


def steps_wire(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step_wire(s, i) for i, s in enumerate(steps or [], start=1)]


def runbook_wire(rb: dict[str, Any], used_by: list[str] | None = None) -> dict[str, Any]:
    """`rb` is our runbook JSON (`app.runbooks.store.runbook_out`)."""
    attached = rb.get("attached") or {}
    steps = rb.get("steps") or []
    reviewed = attached.get("reviewed")
    if reviewed is None:
        reviewed = rb.get("parsedBy") in (None, "deterministic") and not any(
            s.get("suggested") for s in steps
        )
    link = (rb.get("source") or "").strip() if rb.get("format") == "link" else None
    return {
        "id": rb["id"],
        "title": rb["name"],
        "source_format": rb["format"],
        "source_text": rb.get("source") or "",
        "matches": attached.get("matches"),
        "steps": steps_wire(steps),
        "parsed_by": PARSED_BY.get(rb.get("parsedBy") or "", "parser"),
        "reviewed": bool(reviewed),
        "link": link or None,
        "used_by": list(used_by or []),
        "team": rb.get("teamId"),
    }


def parsed_wire(parsed: Any, source: str, runbook_id: str = "draft") -> dict[str, Any]:
    """A parse preview (`ParsedRunbook`) in the `Runbook` shape."""
    steps = [s.model_dump(mode="json", exclude_none=True) for s in parsed.steps]
    link = (
        source.strip()
        if parsed.format == "link"
        else next((s.get("url") for s in steps if s.get("url")), None)
    )
    return {
        "id": runbook_id,
        "title": parsed.name,
        "source_format": parsed.format,
        "source_text": source,
        "matches": parsed.attached.get("matches"),
        "steps": steps_wire(steps),
        "parsed_by": PARSED_BY.get(parsed.parsed_by, "parser"),
        "source": "ai" if parsed.parsed_by == "ai" else "heuristic",
        "reviewed": False,
        "link": link if parsed.format == "link" else None,
        "used_by": [],
    }
