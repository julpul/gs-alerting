"""Read access to a rule definition, whether it is a `RuleDef` model or the plain JSON dict
(`rules.definition`, camelCase). `cfg(rule, "presentation.title_template")` works on both."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic.alias_generators import to_camel


def _step(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj[name] if name in obj else obj.get(to_camel(name))
    value = getattr(obj, name, None)
    return value if value is not None else getattr(obj, to_camel(name), None)


def cfg(rule: Any, path: str, default: Any = None) -> Any:
    """Value at dotted snake_case `path` (`dedup.fields`); enums become their value; None -> `default`."""
    current = rule
    for name in path.split("."):
        current = _step(current, name)
        if current is None:
            return default
    return current.value if isinstance(current, Enum) else current
