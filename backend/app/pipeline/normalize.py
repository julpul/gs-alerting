"""Envelope + source -> `EventView` (raw event + normalized `_meta`), spec 4.4 steps 3-4."""

from __future__ import annotations

from typing import Any

from app.pipeline import injection
from app.pipeline.envelope import parse_received_at
from app.pipeline.fields import EventView
from app.pipeline.mapping import apply_mapping
from app.pipeline.schema_detect import detect_schema


def usable_mapping(source: dict[str, Any]) -> dict[str, Any] | None:
    """A draft (AI-suggested, not yet accepted) mapping is never applied."""
    mapping = source.get("mapping")
    if not isinstance(mapping, dict) or mapping.get("status") == "draft":
        return None
    return mapping


def normalize(envelope: dict[str, Any], source: dict[str, Any]) -> EventView:
    """Typed events arrive pre-normalized; webhook events are detected + mapped here. Both get flags."""
    raw = envelope["raw"]
    if envelope.get("typed") and envelope.get("_meta"):
        meta = dict(envelope["_meta"])
    else:
        meta = apply_mapping(
            raw,
            usable_mapping(source),
            schema=detect_schema(raw),
            source_name=source["name"],
            default_severity=source.get("default_severity") or "warning",
            received_at=parse_received_at(envelope["received_at"]),
        )
    flags: list[str] = []
    if injection.scan(raw).suspicious:
        flags.append(injection.FLAG)
    meta["flags"] = flags
    return EventView(raw=raw, meta=meta)
