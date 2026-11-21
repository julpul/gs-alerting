"""Field path access on raw events: dotted paths with numeric array indices (`alerts.0.labels.severity`).

A `*` segment is a wildcard over a list's elements (or a dict's values): `legs.*.status` yields an `AnyOf`
of every element's `status` (elements without it are skipped; none at all is `MISSING`). Rules treat an
`AnyOf` as "any element matches"; templates join it with ", ". A dict key that is literally `*` wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MISSING = object()
WILDCARD = "*"


class AnyOf(list):
    """The values of a wildcard path (`legs.*.status`), one per element that has the rest of the path."""


def lookup(obj: Any, path: str) -> Any:
    """Value at `path`, or the `MISSING` sentinel (lets rules distinguish `null` from absent)."""
    if path == "":
        return obj
    return _walk(obj, path.split("."))


def _expand(children: Any, rest: list[str]) -> Any:
    found = AnyOf()
    for child in children:
        value = _walk(child, rest)
        if value is MISSING:
            continue
        if isinstance(value, AnyOf):
            found.extend(value)
        else:
            found.append(value)
    return found if found else MISSING


def _walk(obj: Any, parts: list[str]) -> Any:
    current = obj
    for i, part in enumerate(parts):
        if part == WILDCARD and not (isinstance(current, dict) and WILDCARD in current):
            if isinstance(current, list):
                return _expand(current, parts[i + 1 :])
            if isinstance(current, dict):
                return _expand(current.values(), parts[i + 1 :])
            return MISSING
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
