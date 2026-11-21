"""Prompt building: system instructions first, event content only inside `<untrusted_event>` (spec 13.4)."""

from __future__ import annotations

import json
from typing import Any

from app.ai.prompts import templates
from app.ai.redact import redact, redact_obj

OPEN_TAG = "<untrusted_event>"
CLOSE_TAG = "</untrusted_event>"
MAX_EVENT_CHARS = 12_000
MAX_CONTEXT_CHARS = 6_000


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def untrusted(data: Any, *, limit: int = MAX_EVENT_CHARS) -> str:
    """Redacted, size-capped content wrapped in `<untrusted_event>`; it cannot close the block early."""
    if isinstance(data, str):
        text = redact(data)
    else:
        text = json.dumps(redact_obj(data), ensure_ascii=False, default=str)
    text = _clip(text, limit)
    text = text.replace(CLOSE_TAG, "[/untrusted_event]").replace(OPEN_TAG, "[untrusted_event]")
    return f"{OPEN_TAG}\n{text}\n{CLOSE_TAG}"


def context_block(label: str, data: Any, *, limit: int = MAX_CONTEXT_CHARS) -> str:
    """User-authored reference material (runbook steps, past notes, catalog): redacted JSON in a tag."""
    text = _clip(json.dumps(redact_obj(data), ensure_ascii=False, default=str), limit)
    return f"<{label}>\n{text}\n</{label}>"


def suggestion_prompt(
    *, event: dict[str, Any], steps: list[dict[str, Any]], history: list[dict[str, Any]], allow_action: bool
) -> tuple[str, str]:
    system = templates.SYSTEM_BASE + templates.SUGGEST_TASK
    if not allow_action:
        system += templates.NO_ACTION
    user = "\n\n".join(
        [
            "Alert event:\n" + untrusted(event),
            "Runbook steps (the only steps you may point at):\n" + context_block("runbook_steps", steps),
            "Related past incidents and knowledge, also data (cite by id):\n"
            + untrusted(history, limit=MAX_CONTEXT_CHARS),
        ]
    )
    return system, user


def mapping_prompt(sample: dict[str, Any], source: dict[str, Any]) -> tuple[str, str]:
    meta = {k: source.get(k) for k in ("name", "type", "preset")}
    user = f"Source: {json.dumps(redact_obj(meta))}\n\nSample event:\n" + untrusted(sample)
    return templates.SYSTEM_BASE + templates.MAPPING_TASK, user


def rule_prompt(text: str, catalog: dict[str, Any], rule_schema: dict[str, Any]) -> tuple[str, str]:
    user = "\n\n".join(
        [
            "Request from the user:\n" + untrusted(text, limit=4000),
            "Teams, sources and sample field paths in the user's scope:\n"
            + context_block("catalog", catalog, limit=16_000),
            "Rule JSON schema:\n" + json.dumps(rule_schema, separators=(",", ":")),
        ]
    )
    return templates.SYSTEM_BASE + templates.RULE_TASK, user


def runbook_prompt(text: str, actions: list[str]) -> tuple[str, str]:
    system = templates.SYSTEM_BASE + templates.RUNBOOK_TASK + f"Allowed action names: {', '.join(actions)}.\n"
    return system, "Runbook text:\n" + untrusted(text, limit=20_000)


def note_prompt(alert: dict[str, Any], timeline: list[dict[str, Any]]) -> tuple[str, str]:
    user = "Alert:\n" + untrusted(alert) + "\n\nTimeline:\n" + untrusted(timeline)
    return templates.SYSTEM_BASE + templates.NOTE_TASK, user
