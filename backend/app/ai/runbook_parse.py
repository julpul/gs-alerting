"""Free text -> runbook steps (spec 13.3). Action names must exist in the action registry.

When the model is unavailable the text is split into plain text steps (numbered / bulleted lines or
paragraphs), so parsing never fails because of the LLM.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.ai import prompts
from app.ai.llm import AIUnavailable, get_llm

log = logging.getLogger(__name__)

CONTRACT_ACTIONS = ("http_get", "retry_workflow", "restart_service", "webhook", "open_ticket", "escalate")
MAX_STEPS = 30
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")


class ParsedStep(BaseModel):
    text: str
    kind: Literal["text", "check", "action"] = "text"
    action: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk: Literal["low", "medium", "high"] = "low"
    approval: Literal["none", "required"] = "none"


class ParseAnswer(BaseModel):
    steps: list[ParsedStep] = Field(default_factory=list)


def registry_actions() -> list[str]:
    """Action names of `app.runbooks.actions.registry` when importable, else the contract's action set."""
    try:
        from app.runbooks.actions import registry
    except ImportError:
        return list(CONTRACT_ACTIONS)
    for attr in ("REGISTRY", "ACTIONS", "_REGISTRY"):
        found = getattr(registry, attr, None)
        if isinstance(found, dict) and found:
            return sorted(str(k) for k in found)
    for attr in ("names", "action_names", "list_actions"):
        func = getattr(registry, attr, None)
        if callable(func):
            try:
                names = [str(n) for n in func()]
            except Exception:
                break
            if names:
                return sorted(names)
    return list(CONTRACT_ACTIONS)


def _step(order: int, parsed: ParsedStep, allowed: set[str]) -> dict[str, Any]:
    is_action = parsed.kind == "action" and parsed.action in allowed
    risk = parsed.risk if is_action else "low"
    return {
        "id": f"s{order}",
        "order": order,
        "kind": "action" if is_action else ("check" if parsed.kind == "check" else "text"),
        "text": parsed.text.strip(),
        "action": parsed.action if is_action else None,
        "parameters": parsed.parameters if is_action else {},
        "approval": "required" if is_action and (risk != "low" or parsed.approval == "required") else "none",
        "risk": risk,
        "state": "not_started",
    }


def split_plain(text: str) -> list[ParsedStep]:
    """Deterministic fallback: list items, else non-empty paragraphs/lines."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    items = [_LIST_ITEM.sub("", ln).strip() for ln in lines if _LIST_ITEM.match(ln)]
    if not items:
        items = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()] or [ln.strip() for ln in lines]
    return [ParsedStep(text=item) for item in items[:MAX_STEPS]]


async def parse_text(text: str) -> list[dict[str, Any]]:
    """RunbookStep dicts (contract field names) for free-form runbook text."""
    if not text.strip():
        return []
    actions = registry_actions()
    system, user = prompts.runbook_prompt(text, actions)
    try:
        result = await get_llm().complete(system=system, user=user, schema=ParseAnswer, tier="smart")
        steps = [s for s in result.parsed.steps if s.text.strip()][:MAX_STEPS]
    except AIUnavailable as exc:
        log.info("runbook parse without AI: %s", exc)
        steps = []
    steps = steps or split_plain(text)
    allowed = set(actions)
    return [_step(i, s, allowed) for i, s in enumerate(steps, start=1)]
