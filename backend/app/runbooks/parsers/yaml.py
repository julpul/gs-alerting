"""Structured runbooks: YAML (Appendix B `settlement-timeout`) and JSON (`{"steps": [...]}`).

Per step: `check` -> kind check (with its `action` if any); `action` -> kind action; `approval: required`
-> approval; `if: "retry fails twice"` -> guard `previous.failures >= 2` for known phrases, any other
condition is kept as a human instruction ("If <condition>: ..."). Every other key is an action parameter.

Someone else's vocabulary is mapped by a small alias table: the step list may be under `steps`, `tasks`,
`actions`, `procedure`, `playbook`, `checklist` or `items`; a step's text under `name` / `title` /
`description` / `command` / ...; `run` / `uses` name an action (a name that is not in our registry becomes a
manual step that says so, it is never dropped); `needs_ok` / `requires_approval` / `sign_off` mean
approval; `args` / `with` are parameters; `type: human|manual` is a text step.
"""

from __future__ import annotations

from typing import Any

import orjson
import yaml as pyyaml

from app.runbooks.actions.registry import describe, get_action
from app.runbooks.model import ParsedRunbook, finalize, make_step
from app.runbooks.parsers.heuristics import guard_for, step_from_sentence
from app.schemas import api as wire

_TEXT_KEYS = ("check", "text", "title", "step", "description", "do", "name", "task", "label")
_ACTION_ALIASES = ("run", "uses")
_APPROVAL_KEYS = ("approval", "needs_ok", "needsOk", "requires_approval", "requiresApproval", "sign_off")
_PARAM_KEYS = ("parameters", "params", "args", "with")
_RESERVED = {
    *_TEXT_KEYS,
    *_ACTION_ALIASES,
    *_APPROVAL_KEYS,
    *_PARAM_KEYS,
    "action",
    "command",
    "if",
    "when",
    "condition",
    "risk",
    "id",
    "kind",
    "type",
}
STEP_LIST_KEYS = ("steps", "tasks", "actions", "procedure", "playbook", "checklist", "items")
_HUMAN_TYPES = {"human", "manual", "text", "note", "instruction"}


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


def _is_step_item(item: Any) -> bool:
    return (isinstance(item, str) and bool(item.strip())) or (
        isinstance(item, dict) and bool({*_TEXT_KEYS, *_ACTION_ALIASES, "action", "command"} & item.keys())
    )


def step_list(data: Any) -> list[Any] | None:
    """The list of steps in `data` under any alias key (or `data` itself when it is such a list)."""
    if isinstance(data, list):
        return data if data and all(_is_step_item(i) for i in data) else None
    if not isinstance(data, dict):
        return None
    for key in STEP_LIST_KEYS:
        found = data.get(key)
        if isinstance(found, list) and found and all(_is_step_item(i) for i in found):
            return found
    return None


def is_structured(data: Any) -> bool:
    if isinstance(data, list):
        return step_list(data) is not None and all(isinstance(i, dict) for i in data)
    return step_list(data) is not None


def _approval(value: Any) -> str:
    if isinstance(value, bool):
        return "required" if value else "none"
    return "required" if str(value).strip().lower() in {"required", "true", "yes", "needed"} else "none"


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((item[k] for k in keys if k in item and item[k] not in (None, "")), None)


def _step(item: Any) -> wire.RunbookStep:
    if isinstance(item, str):
        return step_from_sentence(item)
    if not isinstance(item, dict):
        raise StructuredError(f"step must be a mapping or a string, got {type(item).__name__}")
    given = _first(item, _PARAM_KEYS)
    params: dict[str, Any] = {**given} if isinstance(given, dict) else {}
    params.update({k: v for k, v in item.items() if k not in _RESERVED})
    action = str(item["action"]) if item.get("action") else None
    base = next((str(item[k]) for k in _TEXT_KEYS if item.get(k)), None)
    alias = _first(item, _ACTION_ALIASES)
    if alias is not None and action is None:
        if get_action(str(alias)) is not None:
            action = str(alias)
        elif str(alias).lower() in ("sleep", "wait"):
            seconds = params.get("seconds") or params.get("duration")
            base = base if base and base.lower() not in ("sleep", "wait") else None
            base = base or (f"Wait {seconds} seconds" if seconds else "Wait")
        else:
            unknown = str(alias)
            params["unregisteredAction"] = unknown
            base = f"{base or unknown} (`{unknown}` is not a registered action: do this by hand)"
    if item.get("command"):
        params.setdefault("command", str(item["command"]))
        base = base or f"Run `{item['command']}` (by hand; never executed)"
    text = base or (describe(action, params) if action else "")
    if not text:
        raise StructuredError("step has no text, check or action")
    condition = item.get("if") or item.get("when") or item.get("condition")
    guard = None
    if condition:
        condition = str(condition)
        guard = guard_for(condition)
        text = f"If {condition}: {text}"
    kind = "check" if item.get("check") else ("action" if action else "text")
    declared = item.get("kind") or item.get("type")
    if declared in ("text", "check", "action"):
        kind = declared if declared != "action" or action else kind
    elif str(declared).lower() in _HUMAN_TYPES and not action:
        kind = "text"
    return make_step(
        text=text,
        kind=kind,
        action=action,
        parameters=params,
        approval=_approval(_first(item, _APPROVAL_KEYS) or "none"),
        risk=item.get("risk") if item.get("risk") in ("low", "medium", "high") else None,
        guard=guard,
        step_id=str(item["id"]) if item.get("id") else None,
    )


def parse(content: str) -> ParsedRunbook:
    data = load(content)
    items = step_list(data)
    if items is None:
        raise StructuredError(f"expected a mapping with a non-empty list under one of {STEP_LIST_KEYS}")
    data = data if isinstance(data, dict) else {}
    attached: dict[str, Any] = {}
    if isinstance(data.get("matches"), dict):
        attached["matches"] = {str(k): v for k, v in data["matches"].items()}
    for key in ("schema", "type", "ruleIds"):
        if data.get(key):
            attached[key] = data[key]
    steps = finalize([_step(i) for i in items])
    names = (data[k] for k in ("runbook", "name", "title", "playbook") if isinstance(data.get(k), str))
    name = str(next(names, None) or "Runbook")
    return ParsedRunbook(name=name, format="yaml", steps=steps, attached=attached)


def is_loose(data: Any) -> bool:
    """A YAML mapping in a shape we do not know (several keys or a list): kept as manual steps."""
    if not isinstance(data, dict) or not data or not all(isinstance(k, str) for k in data):
        return False
    return len(data) >= 2 or any(isinstance(v, list | dict) for v in data.values())


def _compact(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return orjson.dumps(value, default=str).decode()


def _loose_steps(key: str, value: Any) -> list[wire.RunbookStep]:
    if isinstance(value, list):
        out = []
        for item in value:
            try:
                out.append(_step(item))
            except StructuredError:
                out.append(make_step(text=f"{key}: {_compact(item)}"[:2000], kind="text"))
        return out
    return [make_step(text=f"{key}: {_compact(value)}"[:2000], kind="text")]


def loose(content: str) -> ParsedRunbook | None:
    """Unknown YAML keys never crash: every key becomes a manual step ("key: value", list items as steps)."""
    try:
        data = load(content)
    except StructuredError:
        return None
    if not is_loose(data):
        return None
    name = next(
        (str(data[k]) for k in ("runbook", "name", "title", "playbook") if isinstance(data.get(k), str)), None
    )
    skip = {k for k in ("runbook", "name", "title", "playbook") if isinstance(data.get(k), str)}
    steps = [
        st for k, v in data.items() if k not in skip and v not in (None, "") for st in _loose_steps(k, v)
    ]
    if not steps:
        return None
    return ParsedRunbook(name=name or "Runbook", format="yaml", steps=finalize(steps))


def json_document(content: str) -> Any:
    """The parsed JSON object / array, or None when `content` is not JSON."""
    try:
        data = orjson.loads(content)
    except orjson.JSONDecodeError:
        return None
    return data if isinstance(data, dict | list) else None


def raw(content: str) -> ParsedRunbook | None:
    """JSON we cannot map: one manual step showing it formatted (L0), for a person or the AI parser."""
    data = json_document(content)
    if data is None:
        return None
    text = orjson.dumps(data, option=orjson.OPT_INDENT_2).decode()
    named = data.get("name") or data.get("title") if isinstance(data, dict) else None
    step = make_step(text=text[:4000], kind="text")
    return ParsedRunbook(name=str(named or "Runbook"), format="yaml", steps=finalize([step]))
