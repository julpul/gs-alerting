"""Channel selection (spec 9.2), pure functions so the matrix is easy to test.

First non-empty wins:
  1. `rule.routing.timeWindow` - outside the window the rule's `outsideChannels` are used; inside it the
     normal selection continues,
  2. `rule.routing.channelsBySeverity[severity]`,
  3. the recipient's preferences `bySeverity[severity]` (non-critical alerts respect their working hours:
     outside them only non-interrupting channels remain),
  4. the effective policy: the level's channels (hard) or `softChannels` (soft).
Soft recipients never get interrupting channels (sms/phone).
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.delivery.ruleview import RuleView
from app.escalation.policies import EffectivePolicy

log = logging.getLogger(__name__)

INTERRUPTING = frozenset({"sms", "phone"})
KNOWN_CHANNELS = ("ui", "email", "sms", "phone", "webhook")
FALLBACK_CHANNELS = ["email"]


def parse_hhmm(value: Any) -> time | None:
    try:
        hh, _, mm = str(value).partition(":")
        return time(int(hh), int(mm or 0))
    except (TypeError, ValueError):
        return None


def in_window(window: dict[str, Any] | None, now: datetime) -> bool:
    """Is `now` inside `{from|start, to|end, timezone}`? A missing / unparsable window counts as inside."""
    if not window:
        return True
    start = parse_hhmm(window.get("from") or window.get("start"))
    end = parse_hhmm(window.get("to") or window.get("end"))
    if start is None or end is None:
        return True
    try:
        local = now.astimezone(ZoneInfo(window.get("timezone") or "UTC")).time()
    except (KeyError, ValueError):
        log.warning("unknown timezone %r in time window; using UTC", window.get("timezone"))
        local = now.astimezone(ZoneInfo("UTC")).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def _clean(channels: Any) -> list[str]:
    seen: list[str] = []
    for c in channels or []:
        name = str(getattr(c, "value", c))
        if name in KNOWN_CHANNELS and name not in seen:
            seen.append(name)
    return seen


def _from_prefs(prefs: dict[str, Any] | None, severity: str, now: datetime) -> list[str]:
    if not prefs:
        return []
    channels = _clean((prefs.get("by_severity") or {}).get(severity))
    if channels and severity != "critical" and not in_window(prefs.get("working_hours") or None, now):
        channels = [c for c in channels if c not in INTERRUPTING]
    return channels


def select_channels(
    *,
    kind: str,
    severity: str,
    rule: RuleView,
    prefs: dict[str, Any] | None,
    policy: EffectivePolicy,
    level: int,
    now: datetime,
) -> list[str]:
    """Ordered channel names for one recipient (may include `ui`, which routing collapses to the feed)."""
    candidates: list[list[str]] = []
    window = rule.time_window
    if window and not in_window(window, now):
        candidates.append(_clean(window.get("outsideChannels")))
    candidates.append(_clean(rule.channels_by_severity.get(severity)))
    candidates.append(_from_prefs(prefs, severity, now))
    if kind == "hard":
        policy_level = policy.level_for(level)
        candidates.append(_clean(policy_level.channels) if policy_level else [])
    else:
        candidates.append(_clean(policy.soft_channels))

    for channels in candidates:
        if kind == "soft":
            channels = [c for c in channels if c not in INTERRUPTING]
        if channels:
            return channels
    return list(FALLBACK_CHANNELS)
