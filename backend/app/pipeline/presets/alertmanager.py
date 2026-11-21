"""Alertmanager webhook v4: split `alerts[]` into one event per alert; `status: resolved` auto-resolves."""

from __future__ import annotations

from typing import Any

from app.pipeline.fields import get_path

GROUP_KEYS = ("receiver", "externalURL", "groupKey")


def split(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """A grouped payload becomes one event per alert (the alert's own fields win over the group's).

    Every event carries top-level `alertname` (schema detection, rule fields and the fingerprint use it,
    exactly like the single-alert shape of the brief) plus the group's receiver/externalURL/groupKey.
    A payload without an `alerts` list of objects (the single-alert shape) passes through unchanged.
    """
    alerts = raw.get("alerts")
    if not isinstance(alerts, list) or not alerts or not all(isinstance(a, dict) for a in alerts):
        return [raw]
    group = {k: raw[k] for k in GROUP_KEYS if k in raw}
    events = []
    for alert in alerts:
        event = {**group, "status": raw.get("status"), **alert}
        name = event.get("alertname") or get_path(alert, "labels.alertname")
        if name:
            event["alertname"] = name
        events.append(event)
    return events


def is_resolved(raw: dict[str, Any]) -> bool:
    return str(raw.get("status", "")).strip().lower() == "resolved"
