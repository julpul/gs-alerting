"""Effective escalation policy lookup (spec 10.1): rule -> team -> domain -> org, merged per field.

Field semantics when merging stored rows: `levels` / `softChannels` count as set when non-empty,
`renotifySeconds` / `softDigestMinutes` when not NULL. A stored `0` means "explicitly none" at that scope
(and is returned as `None`), so a team can switch off an org-level renotify.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.ids import ORG_ID
from app.delivery.ruleview import rule_view
from app.schemas.api import EffectiveEscalationPolicy, EscalationLevel

SCOPES = ("rule", "team", "domain", "org")
FIELDS = ("levels", "renotifySeconds", "softChannels", "softDigestMinutes")
DEFAULT_SOFT_CHANNELS = ["email", "ui"]
LAST_RESORT_LEVELS = [{"level": 1, "target": "oncall", "channels": ["email"], "ackTimeoutSeconds": None}]


class EffectivePolicy(BaseModel):
    severity: str
    levels: list[EscalationLevel]
    renotify_seconds: int | None = None
    soft_channels: list[str] = Field(default_factory=list)
    soft_digest_minutes: int | None = None
    origin: dict[str, str] = Field(default_factory=dict)

    def level_for(self, level: int) -> EscalationLevel | None:
        """Policy level `level` (1-based); the last defined level for numbers beyond the chain."""
        if not self.levels:
            return None
        for item in self.levels:
            if item.level == level:
                return item
        return self.levels[-1] if level > self.levels[-1].level else None

    def to_wire(self, scope_type: str, scope_id: str) -> EffectiveEscalationPolicy:
        return EffectiveEscalationPolicy.model_validate(
            {
                "scopeType": scope_type,
                "scopeId": scope_id,
                "severity": self.severity,
                "levels": [lv.model_dump(mode="json") for lv in self.levels],
                "renotifySeconds": self.renotify_seconds,
                "softChannels": self.soft_channels,
                "softDigestMinutes": self.soft_digest_minutes,
                "origin": self.origin,
            }
        )


def scaled_seconds(seconds: int) -> int:
    """Policy seconds, shrunk 60x (minutes become seconds) when `DEMO_FAST_ESCALATION` is on."""
    if get_settings().demo_fast_escalation:
        return max(1, seconds // 60)
    return seconds


def _is_set(field: str, value: Any) -> bool:
    if field in ("levels", "softChannels"):
        return bool(value)
    return value is not None


def _field_values(row: Any) -> dict[str, Any]:
    return {
        "levels": row["levels"],
        "renotifySeconds": row["renotify_seconds"],
        "softChannels": list(row["soft_channels"] or []),
        "softDigestMinutes": row["soft_digest_minutes"],
    }


async def effective_policy(
    conn: asyncpg.Connection,
    *,
    rule: Any,
    team_id: UUID,
    domain_id: UUID,
    severity: str,
) -> EffectivePolicy:
    """Merge the policy for (rule, team, domain, org) at `severity`; `origin` names the scope per field."""
    view = rule_view(rule)
    scope_ids = {"rule": view.id, "team": team_id, "domain": domain_id, "org": ORG_ID}
    rows = await conn.fetch(
        "SELECT scope_type, scope_id, levels, renotify_seconds, soft_channels, soft_digest_minutes "
        "FROM escalation_policies WHERE severity = $1 AND scope_id = ANY($2)",
        severity,
        [i for i in scope_ids.values() if i is not None],
    )
    by_scope = {
        r["scope_type"]: _field_values(r) for r in rows if scope_ids.get(r["scope_type"]) == r["scope_id"]
    }

    merged: dict[str, Any] = {}
    origin: dict[str, str] = {}
    for scope in SCOPES:
        values = by_scope.get(scope)
        if values is None:
            continue
        for name in FIELDS:
            if name not in merged and _is_set(name, values[name]):
                merged[name] = values[name]
                origin[name] = scope

    levels = [EscalationLevel.model_validate(lv) for lv in merged.get("levels") or LAST_RESORT_LEVELS]
    origin.setdefault("levels", "org")
    override = view.ack_timeout_by_severity.get(severity)
    if override is not None and levels:
        levels[0] = levels[0].model_copy(update={"ackTimeoutSeconds": override})
        origin["levels"] = "rule"
    soft_channels = merged.get("softChannels") or list(DEFAULT_SOFT_CHANNELS)
    origin.setdefault("softChannels", "org")
    for name in ("renotifySeconds", "softDigestMinutes"):
        origin.setdefault(name, "org")
    return EffectivePolicy(
        severity=severity,
        levels=levels,
        renotify_seconds=merged.get("renotifySeconds") or None,
        soft_channels=[str(c) for c in soft_channels],
        soft_digest_minutes=merged.get("softDigestMinutes") or None,
        origin=origin,
    )
