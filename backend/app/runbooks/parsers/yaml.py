"""Structured runbooks: YAML (Appendix B `settlement-timeout`) and JSON (`{"steps": [...]}`).

Per step: `check` -> kind check (with its `action` if any); `action` -> kind action; `approval: required`
-> approval; `if: "retry fails twice"` -> guard `previous.failures >= 2` for known phrases, any other
condition is kept as a human instruction ("If <condition>: ..."). Every other key is an action parameter.
"""

from __future__ import annotations

from typing import Any

import orjson
import yaml as pyyaml

from app.runbooks.actions.registry import describe
from app.runbooks.model import ParsedRunbook, finalize, make_step
from app.runbooks.parsers.heuristics import guard_for, step_from_sentence
from app.schemas import api as wire

_TEXT_KEYS = ("check", "text", "title", "step", "description", "do", "run", "name")
_RESERVED = {*_TEXT_KEYS, "action", "approval", "if", "when", "risk", "id", "kind", "parameters", "params"}


class StructuredError(ValueError):
    """The content is not a recognisable structured runbook."""


def load(content: str) -> Any:
    try:
        return orjson.loads(content)
    except orjson.JSONDecodeError:
        pass
    try:
        return pyyaml.safe_load(content)
    except pyyaml.YAMLError as exc:
        raise StructuredError(f"invalid YAML: {exc}") from exc


def is_structured(data: Any) -> bool:
    if isinstance(data, dict):
        return isinstance(data.get("steps"), list) or ("runbook" in data and "steps" in data)
    return (
        isinstance(data, list)
        and bool(data)
        and all(isinstance(i, dict) and ({"check", "action"} & i.keys()) for i in data)
    )


def _approval(value: Any) -> str:
    return "required" if str(value).strip().lower() in {"required", "true", "yes", "needed"} else "none"


def _step(item: Any) -> wire.RunbookStep:
    if isinstance(item, str):
        return step_from_sentence(item)
    if not isinstance(item, dict):
        raise StructuredError(f"step must be a mapping or a string, got {type(item).__name__}")
    params: dict[str, Any] = {**(item.get("parameters") or item.get("params") or {})}
    params.update({k: v for k, v in item.items() if k not in _RESERVED})
    action = str(item["action"]) if item.get("action") else None
    base = next((str(item[k]) for k in _TEXT_KEYS if item.get(k)), None)
    text = base or (describe(action, params) if action else "")
    if not text:
        raise StructuredError("step has no text, check or action")
    condition = item.get("if") or item.get("when")
    guard = None
    if condition:
        condition = str(condition)
        guard = guard_for(condition)
        text = f"If {condition}: {text}"
    kind = "check" if item.get("check") else ("action" if action else "text")
    return make_step(
        text=text,
        kind=item.get("kind") if item.get("kind") in ("text", "check", "action") else kind,
        action=action,
        parameters=params,
        approval=_approval(item.get("approval", "none")),
        risk=item.get("risk") if item.get("risk") in ("low", "medium", "high") else None,
        guard=guard,
        step_id=str(item["id"]) if item.get("id") else None,
    )


def parse(content: str) -> ParsedRunbook:
    data = load(content)
    if isinstance(data, list):
        data = {"steps": data}
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list) or not data["steps"]:
        raise StructuredError("expected a mapping with a non-empty `steps` list")
    attached: dict[str, Any] = {}
    if isinstance(data.get("matches"), dict):
        attached["matches"] = {str(k): v for k, v in data["matches"].items()}
    for key in ("schema", "type", "ruleIds"):
        if data.get(key):
            attached[key] = data[key]
    steps = finalize([_step(i) for i in data["steps"]])
    name = str(data.get("runbook") or data.get("name") or data.get("title") or "Runbook")
    return ParsedRunbook(name=name, format="yaml", steps=steps, attached=attached)
