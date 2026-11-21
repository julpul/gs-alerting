"""Action registry (spec 11.3). A new action is one module in this package with a class decorated by
`@register` (see README.md); `load_actions()` imports every module once.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

import asyncpg
from pydantic import BaseModel

from app.pipeline.fields import EventView

Risk = Literal["low", "medium", "high"]
_PACKAGE = "app.runbooks.actions"
_SKIP = {"registry"}
_ACTIONS: dict[str, Action] = {}
_loaded = False


@dataclass
class ActionContext:
    """What an action may use. `conn` is only set for internal actions (escalate); `outbox_ids` collects
    outbox rows the caller must publish after commit."""

    alert: dict[str, Any]
    ev: EventView
    actor: dict[str, Any]
    step_id: str
    run_id: Any
    conn: asyncpg.Connection | None = None
    outbox_ids: list[int] = field(default_factory=list)


@dataclass
class ActionResult:
    ok: bool
    output: str
    data: dict[str, Any] = field(default_factory=dict)


class Action(Protocol):
    name: ClassVar[str]
    params_schema: ClassVar[type[BaseModel]]
    default_risk: ClassVar[Risk]
    label: ClassVar[str]
    affects_trust: ClassVar[bool]

    async def run(self, params: Any, ctx: ActionContext) -> ActionResult: ...


def register[T: type](cls: T) -> T:
    """Class decorator: instantiate the action and make it the handler of `cls.name`."""
    _ACTIONS[cls.name] = cls()  # type: ignore[attr-defined]
    return cls


def load_actions() -> None:
    global _loaded
    if _loaded:
        return
    package = importlib.import_module(_PACKAGE)
    for info in pkgutil.iter_modules(package.__path__):
        if info.name not in _SKIP and not info.name.startswith("_"):
            importlib.import_module(f"{_PACKAGE}.{info.name}")
    _loaded = True


def get_action(name: str) -> Action | None:
    load_actions()
    return _ACTIONS.get(name)


def actions() -> dict[str, Action]:
    load_actions()
    return dict(_ACTIONS)


def default_risk(name: str | None) -> Risk | None:
    action = get_action(name) if name else None
    return action.default_risk if action else None


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return ""


def describe(name: str, params: dict[str, Any]) -> str:
    """Human label of an action step, e.g. `Retry workflow {{workflowId}}`."""
    action = get_action(name)
    if action is None:
        return name
    try:
        return " ".join(action.label.format_map(_Safe(params)).split())
    except (ValueError, IndexError, KeyError):
        return action.label


def override(name: str, action: Action) -> Callable[[], None]:
    """Swap an action (tests); returns a function restoring the previous one."""
    load_actions()
    previous = _ACTIONS.get(name)
    _ACTIONS[name] = action

    def restore() -> None:
        if previous is None:
            _ACTIONS.pop(name, None)
        else:
            _ACTIONS[name] = previous

    return restore
