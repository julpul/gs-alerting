"""Why did (or didn't) a rule match an event, in Herald's `Why` tree shape, with OUR compiler's semantics.

The tree is HB-CORE's `views.explain` over our `match` (leaves evaluated by our compiler, so `passed` at
the root equals what the worker decides). Herald `when` documents are translated first (`dsl`). Also:
turning whatever the UI sends as "an event" into `EventView`s like the worker does.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import orjson

from app.herald.dsl import DslError, when_to_match
from app.herald.views import explain
from app.pipeline.envelope import build_envelope
from app.pipeline.fields import EventView
from app.pipeline.normalize import normalize
from app.pipeline.presets import split_events
from app.pipeline.rules.model import RuleDef

ATTRS = ("type", "severity", "timestamp", "service")
NO_SOURCE = {
    "id": UUID(int=0),
    "name": "herald",
    "mapping": None,
    "default_severity": "warning",
    "preset": None,
}


def raw_events(event: Any, source: dict[str, Any] | None) -> list[dict[str, Any]]:
    """An object, a batch (list / Alertmanager body) or a text line -> raw event objects."""
    if isinstance(event, str):
        try:
            event = orjson.loads(event)
        except orjson.JSONDecodeError:
            event = {"message": event.strip()}
    items = event if isinstance(event, list) else [event]
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            item = {"value": item}
        out += split_events((source or {}).get("preset"), item)
    return out


def event_view(raw: dict[str, Any], source: dict[str, Any] | None) -> EventView:
    """Normalize like the worker: schema detection + the source's accepted mapping."""
    src = dict(source or NO_SOURCE)
    status = src.pop("mapping_status", None)
    if isinstance(src.get("mapping"), dict) and status:
        src["mapping"] = {**src["mapping"], "status": status}
    envelope = build_envelope(source_id=src["id"], raw=raw, priority=3)
    return normalize(envelope, src)


def attrs(ev: EventView) -> dict[str, Any]:
    return {k: ev.meta.get(k) for k in ATTRS}


def explain_when(when: dict[str, Any], ev: EventView) -> dict[str, Any]:
    """A Herald `when` against one event: translated to our `match`, then HB-CORE's `views.explain`
    (every leaf evaluated by our compiler). Raises `DslError` when `when` is not valid."""
    why = explain(when_to_match(when), ev)
    if why is None:
        raise DslError("the condition is not valid")
    return why


def failing_leaves(explained: dict[str, Any]) -> list[dict[str, Any]]:
    """The conditions that stopped a match (Herald's one-line "failed because")."""
    if explained["passed"]:
        return []
    for key in ("all", "any"):
        if key in explained:
            return [leaf for child in explained[key] for leaf in failing_leaves(child)]
    if "not" in explained:
        return [{"not": explained["not"], "passed": False}]
    return [explained]


def closes(resolve_when: dict[str, Any] | None, ev: EventView) -> bool:
    """Would this event close the rule's open alert (`resolve_when`, stored but advisory)?"""
    if not resolve_when:
        return False
    try:
        return bool(explain_when(resolve_when, ev)["passed"])
    except DslError:
        return False


def explain_rule(rule: RuleDef, ev: EventView, resolve_when: dict[str, Any] | None) -> dict[str, Any]:
    why = explain(rule.definition()["match"], ev) or {"passed": False}
    return {
        "passed": why["passed"],
        "closes": closes(resolve_when, ev),
        "why": why,
        "failed_because": failing_leaves(why),
    }
