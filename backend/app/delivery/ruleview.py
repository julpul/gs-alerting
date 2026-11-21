"""Tolerant read-only view over a rule: works with a `RuleDef`-like object and with the raw camelCase dict.

Only the routing-related parts of a rule matter for delivery; everything else is ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.ids import parse_uuid

DEFAULT_MAX_PER_MINUTE = 10


@dataclass(frozen=True)
class RecipientSpec:
    kind: str
    id: str


@dataclass(frozen=True)
class RuleView:
    id: UUID | None = None
    team_id: UUID | None = None
    hard: list[RecipientSpec] = field(default_factory=list)
    soft: list[RecipientSpec] = field(default_factory=list)
    channels_by_severity: dict[str, list[str]] = field(default_factory=dict)
    time_window: dict[str, Any] | None = None
    max_per_minute: int = DEFAULT_MAX_PER_MINUTE
    ack_timeout_by_severity: dict[str, int] = field(default_factory=dict)
    escalation_path: list[RecipientSpec] = field(default_factory=list)


def _as_dict(rule: Any) -> dict[str, Any]:
    if rule is None:
        return {}
    if isinstance(rule, dict):
        data = dict(rule)
    elif hasattr(rule, "model_dump"):
        data = rule.model_dump(mode="json", by_alias=True)
    else:
        data = {
            k: getattr(rule, k)
            for k in ("id", "team_id", "routing", "throttle", "escalation")
            if hasattr(rule, k)
        }
    for key in ("id", "team_id"):
        if key not in data and hasattr(rule, key):
            data[key] = getattr(rule, key)
    return data


def _spec(item: Any) -> RecipientSpec | None:
    if isinstance(item, str):
        kind, _, ident = item.partition(":")
        return RecipientSpec(kind.strip(), ident.strip()) if kind.strip() else None
    if isinstance(item, dict):
        kind, ident = item.get("kind"), item.get("id")
    else:
        kind, ident = getattr(item, "kind", None), getattr(item, "id", None)
    kind = getattr(kind, "value", kind)
    if not kind:
        return None
    return RecipientSpec(str(kind), "" if ident is None else str(ident))


def parse_specs(items: Any) -> list[RecipientSpec]:
    """`[{kind, id}, "oncall:team-rates", ...]` -> specs (unknown shapes are skipped)."""
    return [s for s in (_spec(i) for i in items or []) if s is not None]


def _uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    return parse_uuid(str(value)) if value else None


def rule_view(rule: Any) -> RuleView:
    """Build a `RuleView` from a RuleDef, a camelCase definition dict (optionally with id/teamId) or None."""
    data = _as_dict(rule)
    routing = data.get("routing") or {}
    if not isinstance(routing, dict):
        routing = {}
    escalation = data.get("escalation") or {}
    throttle = data.get("throttle") or {}
    window = routing.get("timeWindow") or routing.get("time_window")
    return RuleView(
        id=_uuid(data.get("id")),
        team_id=_uuid(data.get("teamId") or data.get("team_id")),
        hard=parse_specs(routing.get("hard")),
        soft=parse_specs(routing.get("soft")),
        channels_by_severity={
            sev: [str(getattr(c, "value", c)) for c in chans]
            for sev, chans in (routing.get("channelsBySeverity") or {}).items()
        },
        time_window=dict(window) if window else None,
        max_per_minute=int(throttle.get("maxPerRecipientPerMinute") or DEFAULT_MAX_PER_MINUTE),
        ack_timeout_by_severity={
            sev: int(v) for sev, v in (escalation.get("ackTimeoutSecondsBySeverity") or {}).items()
        },
        escalation_path=parse_specs(escalation.get("path")),
    )
