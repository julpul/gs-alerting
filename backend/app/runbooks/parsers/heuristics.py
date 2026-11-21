"""Deterministic verb heuristics shared by the Markdown, checklist and free-text parsers.

A sentence that names a known operation becomes an action step whose mapping is only a guess:
`suggested: true`, `parsedBy: heuristic`. Nothing here ever runs; running goes through approvals.
"""

from __future__ import annotations

import re
from typing import Any

from app.runbooks.model import make_step
from app.schemas import api as wire

_I = re.IGNORECASE
# (pattern, action, parameters). Order matters: the first match wins.
VERBS: list[tuple[re.Pattern[str], str, dict[str, Any]]] = [
    (re.compile(r"\bfail(?:ing|ed)?\s+(?:\w+\s+){0,2}over\b|\bfail-?over\b", _I), "failover_region", {}),
    (re.compile(r"\broll(?:ing)?\s+(?:\w+\s+){0,2}back\b|\brollback\b", _I), "rollback_deploy", {}),
    (re.compile(r"\brestart(?:ing)?\b", _I), "restart_service", {"service": "{{_service}}"}),
    (re.compile(r"\bre-?(?:try|run)\b", _I), "retry_workflow", {"workflowId": "{{workflowId}}"}),
    (re.compile(r"\bopen\s+(?:a\s+|an\s+)?(?:ticket|jira)\b", _I), "open_ticket", {"summary": "{{_title}}"}),
    (re.compile(r"\bescalate\b|\b(?:call|page|phone|ring)\b[^.;]*\bon-?call\b", _I), "escalate", {}),
]
APPROVAL = re.compile(r"\b(?:needs?|requires?)\s+(?:\w+\s+)?approval\b|\bapproval\s+required\b", _I)
_CONDITIONAL = re.compile(r"^\s*(?:and\s+)?if\b", _I)
_FAILS = re.compile(
    r"\b(?:retry|it|that|this|the previous step)\s+(?:fails|failed)(?:\s+(twice|once|(\d+)\s+times))?", _I
)


def guard_for(condition: str) -> str | None:
    """Known phrases ("retry fails twice") -> `previous.failures >= N`; anything else None (human text)."""
    m = _FAILS.search(condition)
    if m is None:
        return None
    count = {"twice": 2, "once": 1}.get((m.group(1) or "").lower()) or int(m.group(2) or 1)
    return f"previous.failures >= {count}"


def suggest_action(sentence: str) -> tuple[str, dict[str, Any]] | None:
    for pattern, action, params in VERBS:
        if pattern.search(sentence):
            return action, dict(params)
    return None


def needs_approval(sentence: str) -> bool:
    return APPROVAL.search(sentence) is not None


def is_conditional(sentence: str) -> bool:
    return _CONDITIONAL.match(sentence) is not None


def step_from_sentence(sentence: str, *, parsed_by: str = "heuristic") -> wire.RunbookStep:
    """One human sentence -> a text step, or an action step when a known verb is recognised."""
    text = " ".join(sentence.split())
    approval = "required" if needs_approval(text) else "none"
    found = suggest_action(text)
    if found is None:
        return make_step(text=text, kind="text", approval=approval, parsed_by="deterministic")
    action, params = found
    return make_step(
        text=text,
        kind="action",
        action=action,
        parameters=params,
        approval=approval,
        suggested=True,
        parsed_by=parsed_by,
    )
