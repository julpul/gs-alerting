"""Deterministic mapping draft (spec 6): the degradation path when the LLM is unavailable.

Looks at key names (severity/level/priority, service/producer/app, env, ts/timestamp, message/summary/...)
of a sample event and proposes a `SchemaMapping` draft. Every proposed path exists in the sample.
"""

from __future__ import annotations

import hashlib
from typing import Any

MAX_DEPTH = 5

# target -> key names (normalized: lowercase, no `_`/`-`), best first
KEY_NAMES: dict[str, tuple[str, ...]] = {
    "severity": ("severity", "level", "loglevel", "priority", "sev", "urgency", "alerttype"),
    "service": (
        "service",
        "servicename",
        "producer",
        "app",
        "application",
        "component",
        "logger",
        "workflowname",
        "project",
        "hostname",
        "host",
        "source",
    ),
    "environment": ("environment", "env", "stage", "deployment"),
    "timestamp": (
        "timestamp",
        "ts",
        "time",
        "startsat",
        "datetime",
        "eventtime",
        "occurredat",
        "createdat",
        "date",
    ),
    "title": ("summary", "title", "message", "msg", "description", "subject", "text"),
    "type": ("type", "eventtype", "kind", "alertname", "schema", "event"),
    "error_code": ("errorcode", "errcode", "code"),
}

SEVERITY_WORDS = {
    "CRITICAL": "critical",
    "FATAL": "critical",
    "SEVERE": "critical",
    "ERROR": "error",
    "WARN": "warning",
    "WARNING": "warning",
    "INFO": "info",
    "DEBUG": "info",
}
PRIORITY_NUMBERS = {"1": "critical", "2": "error", "3": "warning", "4": "info", "5": "info"}
EXPLANATION_PREFIX = "AI unavailable, heuristic draft."


def _leaves(obj: Any, prefix: str = "", depth: int = 0):
    """Yield `(path, value)` of scalar leaves; arrays contribute their first element only."""
    if depth > MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _leaves(value, f"{prefix}.{key}" if prefix else str(key), depth + 1)
    elif isinstance(obj, list):
        if obj:
            yield from _leaves(obj[0], f"{prefix}.0" if prefix else "0", depth + 1)
    elif prefix:
        yield prefix, obj


def _norm(key: str) -> str:
    return key.replace("_", "").replace("-", "").lower()


def detect_schema(sample: dict[str, Any]) -> str:
    """meta.schema -> type -> alertname -> schema -> hash of the sorted top-level keys."""
    meta = sample.get("meta")
    candidates = [meta.get("schema") if isinstance(meta, dict) else None]
    candidates += [sample.get(k) for k in ("type", "alertname", "schema")]
    for value in candidates:
        if isinstance(value, str) and value:
            return value
    digest = hashlib.sha1(",".join(sorted(sample)).encode()).hexdigest()[:8]
    return f"keys:{digest}"


def _accepts(target: str, value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if target in ("severity", "service", "environment", "title", "type"):
        return isinstance(value, str | int) and value != ""
    return True


def _pick(target: str, leaves: list[tuple[str, Any]]) -> str | None:
    names = KEY_NAMES[target]
    best: tuple[int, int, int] | None = None
    best_path: str | None = None
    for order, (path, value) in enumerate(leaves):
        last = _norm(path.rsplit(".", 1)[-1])
        if last not in names or not _accepts(target, value):
            continue
        rank = (names.index(last), path.count("."), order)
        if best is None or rank < best:
            best, best_path = rank, path
    return best_path


def _severity_map(value: Any) -> dict[str, str]:
    mapping = dict(SEVERITY_WORDS)
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        mapping.update(PRIORITY_NUMBERS)
    elif isinstance(value, str) and value.upper() in SEVERITY_WORDS:
        mapping[value] = SEVERITY_WORDS[value.upper()]
    return mapping


def suggest_mapping_heuristic(sample: dict[str, Any]) -> dict[str, Any]:
    leaves = list(_leaves(sample))
    values = dict(leaves)
    fields: dict[str, str] = {}
    for target in KEY_NAMES:
        path = _pick(target, leaves)
        if path is not None:
            fields[target] = path
    mapping: dict[str, Any] = {
        "status": "draft",
        "detectedSchema": detect_schema(sample),
        "fields": fields,
    }
    if "severity" in fields:
        mapping["severityValueMap"] = _severity_map(values[fields["severity"]])
    found = ", ".join(f"{k} <- {v}" for k, v in fields.items()) or "no known field names found"
    mapping["explanation"] = f"{EXPLANATION_PREFIX} Matched by key names: {found}."
    return mapping
