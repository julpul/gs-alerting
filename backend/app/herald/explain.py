"""Why did (or didn't) a rule match an event, in Herald's `Why` tree shape, with OUR compiler's semantics.

The tree walked is the Herald `when` derived from the rule's `match` (`dsl.match_to_when`); every leaf is
evaluated by compiling it with `app.pipeline.rules.compiler`, so `passed` at the root equals what the
worker decides. Also: turning whatever the UI sends as "an event" into `EventView`s like the worker does.

HB-CORE's alert detail (`views.explain`) does the same for one alert; the two can converge on this module.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import orjson

from app.herald.dsl import DslError, expand, field_to_ours, leaf_to_condition, match_to_when
from app.pipeline.envelope import build_envelope
from app.pipeline.fields import MISSING, EventView
from app.pipeline.normalize import normalize
from app.pipeline.presets import split_events
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.model import RuleDef, RuleValidationError

ATTRS = ("type", "severity", "timestamp", "service")
NO_SOURCE = {
    "id": UUID(int=0),
    "name": "herald",
    "mapping": None,
    "default_severity": "warning",
    "preset": None,
}
_TEAM = UUID(int=0)


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


class LeafCache:
    """Compiled single-condition predicates, keyed by the condition JSON."""

    def __init__(self) -> None:
        self._items: dict[bytes, Any] = {}

    def predicate(self, cond: dict[str, Any]) -> Any:
        key = orjson.dumps(cond, option=orjson.OPT_SORT_KEYS)
        if key not in self._items:
            rule = RuleDef(name="explain", teamId=_TEAM, match={"op": "and", "conditions": [cond]})
            self._items[key] = compile_rule(rule)
        return self._items[key]


def explain_when(when: dict[str, Any], ev: EventView, cache: LeafCache | None = None) -> dict[str, Any]:
    """The Herald tree with `passed` on every node, `actual`/`missing` on leaves. Raises `DslError`."""
    return _walk(expand(when), ev, cache or LeafCache())


def _walk(node: dict[str, Any], ev: EventView, cache: LeafCache) -> dict[str, Any]:
    for key, combine in (("all", all), ("any", any)):
        if key in node:
            children = [_walk(c, ev, cache) for c in node[key]]
            return {key: children, "passed": combine(c["passed"] for c in children)}
    if "not" in node:
        inner = _walk(node["not"], ev, cache)
        return {"not": inner, "passed": not inner["passed"]}
    cond = leaf_to_condition(node)
    try:
        passed = cache.predicate(cond).matches(ev)
    except RuleValidationError as exc:
        raise DslError(f"{node['field']}: {exc.message}") from exc
    actual = ev.lookup(field_to_ours(node["field"]))
    missing = actual is MISSING
    return {**node, "actual": None if missing else actual, "missing": missing, "passed": passed}


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


def explain_rule(
    rule: RuleDef, ev: EventView, resolve_when: dict[str, Any] | None, cache: LeafCache
) -> dict[str, Any]:
    why = explain_when(match_to_when(rule.definition()["match"]), ev, cache)
    closes = False
    if resolve_when:
        try:
            closes = explain_when(resolve_when, ev, cache)["passed"]
        except DslError:
            closes = False
    return {"passed": why["passed"], "closes": closes, "why": why, "failed_because": failing_leaves(why)}
