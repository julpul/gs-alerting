"""Runbook step model = the contract `RunbookStep`; parsers produce it, `runbooks.steps` stores it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.runbooks.actions.registry import default_risk
from app.schemas import api as wire

MAX_STEPS = 100
MAX_CONTENT_BYTES = 100_000
RUNTIME_FIELDS = {"state", "lastRun", "trustLevel", "approvalId", "guardMet"}


class RunbookView(wire.Runbook):
    """Runbook as returned by `runbook_for_alert`: steps rendered with the alert's values, with run state."""

    steps: list[wire.RunbookStep] = []


@dataclass
class ParsedRunbook:
    name: str
    format: str
    steps: list[wire.RunbookStep]
    attached: dict[str, Any] = field(default_factory=dict)
    parsed_by: str = "deterministic"


def make_step(
    *,
    text: str,
    kind: str = "text",
    action: str | None = None,
    parameters: dict[str, Any] | None = None,
    approval: str = "none",
    risk: str | None = None,
    guard: str | None = None,
    suggested: bool = False,
    url: str | None = None,
    parsed_by: str = "deterministic",
    step_id: str | None = None,
) -> wire.RunbookStep:
    return wire.RunbookStep.model_validate(
        {
            "id": step_id,
            "kind": kind,
            "text": text.strip(),
            "action": action,
            "parameters": parameters or {},
            "approval": approval,
            "risk": risk,
            "guard": guard,
            "suggested": suggested,
            "url": url,
            "parsedBy": parsed_by,
        }
    )


def finalize(steps: list[wire.RunbookStep]) -> list[wire.RunbookStep]:
    """Number the steps, make ids unique (`s1`, `s2`, ...) and fill the default risk of action steps."""
    seen: set[str] = set()
    out: list[wire.RunbookStep] = []
    for order, step in enumerate(steps[:MAX_STEPS], start=1):
        step_id = step.id if step.id and step.id not in seen else f"s{order}"
        while step_id in seen:
            step_id += "x"
        seen.add(step_id)
        risk = step.risk.value if step.risk else default_risk(step.action)
        out.append(
            step.model_copy(
                update={
                    "id": step_id,
                    "order": order,
                    "risk": wire.Risk(risk) if risk else None,
                    "approval": step.approval or wire.Approval1.none,
                    "kind": step.kind or wire.Kind5.text,
                    "parameters": step.parameters or {},
                }
            )
        )
    return out


def to_stored(step: wire.RunbookStep) -> dict[str, Any]:
    """JSON kept in `runbooks.steps`: the unrendered definition without per-alert state."""
    return step.model_dump(mode="json", exclude_none=True, exclude=RUNTIME_FIELDS)


def from_stored(data: dict[str, Any]) -> wire.RunbookStep:
    return wire.RunbookStep.model_validate(data)
