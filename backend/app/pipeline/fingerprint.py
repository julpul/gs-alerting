"""Dedup fingerprint (spec 8) and knowledge key (spec 12)."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from typing import Any
from uuid import UUID

import orjson

SEPARATOR = "\x1f"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode()


def compute_fingerprint(
    rule_id: UUID | str,
    values: Iterable[Any],
    *,
    group_value: Any = None,
    window_seconds: int | None = None,
    now: float | None = None,
) -> str:
    """sha1(rule_id + dedup values); with `window_seconds` the group key and `floor(now / window)` join in."""
    parts = [str(rule_id), *(_text(v) for v in values)]
    if window_seconds:
        bucket = int((time.time() if now is None else now) // window_seconds)
        parts += ["group", _text(group_value), str(bucket)]
    return hashlib.sha1(SEPARATOR.join(parts).encode()).hexdigest()


def knowledge_key(
    domain_id: UUID | str, type_or_schema: str | None, service: str | None, error_code: str | None
) -> str:
    """sha1(domain_id + (_type or _schema) + _service + _error_code)."""
    raw = SEPARATOR.join([str(domain_id), type_or_schema or "", service or "", error_code or ""])
    return hashlib.sha1(raw.encode()).hexdigest()
