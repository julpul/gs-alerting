"""Our runbooks / runbook steps in Herald's `Runbook` and `Step` shapes.

Step id = the step's 1-based position (Herald uses ints); our own step id (`s1`, ...) is kept as `key`.
Kinds: our `text` -> Herald `manual`; `check`, `action`, `wait` are the same. `guard` -> `condition`.
A step whose action mapping was guessed (`suggested`) shows it as `suggest` (never run until reviewed).
"""

from __future__ import annotations

from typing import Any

PARSED_BY = {"ai": "llm", "llm": "llm", "human": "human"}
KINDS = {"check": "check", "action": "action", "wait": "wait"}


def step_wire(step: dict[str, Any], position: int) -> dict[str, Any]:
    suggested = bool(step.get("suggested"))
    action = step.get("action")
    return {
        "id": position,
        "key": step.get("id"),
        "kind": KINDS.get(step.get("kind") or "", "action" if action and not suggested else "manual"),
        "text": step.get("text") or "",
        "action": None if suggested else action,
        "params": step.get("parameters") or {},
        "approval": step.get("approval") or "none",
        "condition": step.get("guard"),
        "suggest": action if suggested else None,
        "url": step.get("url"),
    }


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
        "reviewed": False,
        "link": link if parsed.format == "link" else None,
        "used_by": [],
    }
