"""Applying a source's schema mapping (spec 6): common fields, severity value map, timestamp parsing.

A mapping's `fields` name the path of each common field in the raw event. A field the mapping does not
cover (or no mapping at all) falls back to well-known paths, so unmapped sources still produce useful
`_meta`; a severity that is not recognizable falls back to the source default, then `warning`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.pipeline.envelope import SEVERITY_RANK, format_ts
from app.pipeline.fields import get_path

SEVERITY_WORDS = {
    "critical": "critical",
    "crit": "critical",
    "fatal": "critical",
    "emergency": "critical",
    "emerg": "critical",
    "alert": "critical",
    "page": "critical",
    "p1": "critical",
    "sev1": "critical",
    "error": "error",
    "err": "error",
    "severe": "error",
    "major": "error",
    "high": "error",
    "p2": "error",
    "sev2": "error",
    "warning": "warning",
    "warn": "warning",
    "medium": "warning",
    "p3": "warning",
    "sev3": "warning",
    "info": "info",
    "information": "info",
    "notice": "info",
    "low": "info",
    "debug": "info",
    "trace": "info",
    "p4": "info",
    "sev4": "info",
}

FALLBACK_PATHS: dict[str, tuple[str, ...]] = {
    "severity": ("severity", "level", "labels.severity", "priority", "log.level"),
    "service": ("service", "labels.service", "meta.producer", "producer", "app", "logger", "workflowName"),
    "environment": ("environment", "env", "labels.environment", "labels.env"),
    "timestamp": ("timestamp", "ts", "time", "@timestamp", "meta.ts", "startsAt"),
    "title": ("title", "annotations.summary", "summary", "message", "msg", "error.message"),
    "type": ("type", "eventType", "event_type"),
    "error_code": ("error.code", "errorCode", "error_code", "code"),
    "source": ("meta.producer", "producer", "logger", "source"),
}


def normalize_severity(value: Any, value_map: dict[str, str] | None = None) -> str | None:
    """Map a raw severity to critical|error|warning|info, honouring the mapping's `severityValueMap`."""
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    if value_map:
        for candidate in (text, text.upper(), text.lower()):
            mapped = value_map.get(candidate)
            if mapped in SEVERITY_RANK:
                return mapped
    return SEVERITY_WORDS.get(text.lower())


def parse_timestamp(value: Any) -> datetime | None:
    """ISO-8601, or epoch seconds / milliseconds (numbers or numeric strings), as an aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    number: float | None = None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    else:
        return None
    seconds = number / 1000 if abs(number) >= 1e11 else number
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _as_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def _mapped(raw: dict[str, Any], fields: dict[str, Any], name: str) -> Any:
    path = fields.get(name)
    return get_path(raw, path) if isinstance(path, str) and path else None


def apply_mapping(
    raw: dict[str, Any],
    mapping: dict[str, Any] | None,
    *,
    schema: str,
    source_name: str,
    default_severity: str,
    received_at: datetime,
) -> dict[str, Any]:
    """Build the `_meta` common fields of an event (everything except flags)."""
    fields = (mapping or {}).get("fields") or {}
    value_map = (mapping or {}).get("severityValueMap") or {}

    def text(name: str) -> str | None:
        found = _as_text(_mapped(raw, fields, name))
        for path in FALLBACK_PATHS[name]:
            if found:
                break
            found = _as_text(get_path(raw, path))
        return found

    severity = normalize_severity(_mapped(raw, fields, "severity"), value_map)
    if severity is None:
        for path in FALLBACK_PATHS["severity"]:
            severity = normalize_severity(get_path(raw, path), value_map)
            if severity:
                break
    stamp = parse_timestamp(_mapped(raw, fields, "timestamp"))
    if stamp is None:
        for path in FALLBACK_PATHS["timestamp"]:
            stamp = parse_timestamp(get_path(raw, path))
            if stamp:
                break
    return {
        "source": text("source") or source_name,
        "source_name": source_name,
        "schema": schema,
        "severity": severity or default_severity or "warning",
        "service": text("service"),
        "environment": text("environment"),
        "type": text("type"),
        "title": text("title"),
        "timestamp": format_ts(stamp or received_at),
        "error_code": text("error_code"),
    }
