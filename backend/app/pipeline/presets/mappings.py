"""Pre-filled schema mappings of the source presets (spec 5.2). Data only; the worker applies them.

Paths follow each tool's public webhook format. Alertmanager events are one `alerts[]` entry each
(see `alertmanager.split`), so its paths are relative to a single alert.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_SEVERITY_WORDS = {
    "critical": "critical",
    "fatal": "critical",
    "error": "error",
    "warning": "warning",
    "warn": "warning",
    "info": "info",
    "debug": "info",
}

PRESET_MAPPINGS: dict[str, dict[str, Any]] = {
    "alertmanager": {
        "status": "accepted",
        "detectedSchema": "alertmanager",
        "fields": {
            "severity": "labels.severity",
            "service": "labels.service",
            "environment": "labels.environment",
            "timestamp": "startsAt",
            "title": "annotations.summary",
            "type": "alertname",  # top-level: the single-alert shape has it; `alertmanager.split` adds it
        },
        "severityValueMap": {**_SEVERITY_WORDS, "page": "critical"},
        "explanation": "Alertmanager preset: one event per `alerts[]` entry; resolved auto-resolves.",
    },
    "sentry": {
        "status": "accepted",
        "detectedSchema": "sentry",
        "fields": {
            "severity": "data.event.level",
            "service": "data.event.project",
            "environment": "data.event.environment",
            "timestamp": "data.event.timestamp",
            "title": "data.event.title",
            "type": "data.triggered_rule",
        },
        "severityValueMap": _SEVERITY_WORDS,
        "explanation": "Sentry issue-alert webhook preset.",
    },
    "datadog": {
        "status": "accepted",
        "detectedSchema": "datadog",
        "fields": {
            "severity": "alert_type",
            "service": "hostname",
            "environment": "env",
            "timestamp": "date",
            "title": "title",
            "type": "event_type",
        },
        "severityValueMap": {**_SEVERITY_WORDS, "success": "info"},
        "explanation": "Datadog webhook preset.",
    },
    "cloudevents": {
        "status": "accepted",
        "detectedSchema": "cloudevents",
        "fields": {
            "severity": "data.severity",
            "service": "source",
            "environment": "data.environment",
            "timestamp": "time",
            "title": "data.message",
            "type": "type",
        },
        "severityValueMap": _SEVERITY_WORDS,
        "explanation": "CloudEvents 1.0 envelope preset.",
    },
}


def preset_mapping(preset: str | None) -> dict[str, Any] | None:
    mapping = PRESET_MAPPINGS.get(preset or "")
    return deepcopy(mapping) if mapping else None
