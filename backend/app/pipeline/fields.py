"""Field path access on raw events: dotted paths with numeric array indices (`alerts.0.labels.severity`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MISSING = object()


def lookup(obj: Any, path: str) -> Any:
    """Value at `path`, or the `MISSING` sentinel (lets rules distinguish `null` from absent)."""
    if path == "":
        return obj
    current = obj
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return MISSING
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                return MISSING
            current = current[int(part)]
        else:
            return MISSING
    return current


def get_path(obj: Any, path: str) -> Any:
    """Value at `path`; a missing path yields None."""
    value = lookup(obj, path)
    return None if value is MISSING else value


@dataclass
class EventView:
    """An event as rules see it: `_x` addresses normalized `_meta` fields, anything else the raw JSON."""

    raw: dict
    meta: dict = field(default_factory=dict)

    def lookup(self, field_name: str) -> Any:
        if field_name.startswith("_"):
            return self.meta.get(field_name[1:], MISSING)
        return lookup(self.raw, field_name)

    def get(self, field_name: str) -> Any:
        value = self.lookup(field_name)
        return None if value is MISSING else value
