"""Deterministic schema detection (spec 6): meta.schema -> type -> alertname -> schema -> key-set hash."""

from __future__ import annotations

import hashlib
from typing import Any

from app.pipeline.fields import get_path

SCHEMA_PATHS = ("meta.schema", "type", "alertname", "schema")


def keys_signature(raw: dict[str, Any]) -> str:
    digest = hashlib.sha1("\x1f".join(sorted(raw)).encode()).hexdigest()[:8]
    return f"keys:{digest}"


def detect_schema(raw: dict[str, Any]) -> str:
    for path in SCHEMA_PATHS:
        value = get_path(raw, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return keys_signature(raw)
