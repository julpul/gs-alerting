"""Identifiers: UUIDv7 for runtime rows, deterministic UUIDv5 for seed data, short alert ids (A-1234)."""

from __future__ import annotations

import re
import secrets
from uuid import UUID, uuid5

from uuid6 import uuid7 as _uuid7

# Fixed id of the (single) organization. Used as `scope_id` of org-level role assignments and as
# `scope_id` of org-level escalation policies, so that scope_id is never NULL.
ORG_ID = UUID("00000000-0000-4000-8000-000000000001")

# Namespace for deterministic seed ids: stable_id("user", "anna@demo") is the same on every machine.
SEED_NAMESPACE = UUID("6f1c2e0a-5b0e-4f7e-9d0c-3a8b1d2c4e5f")

_SHORT_ID_RE = re.compile(r"^[Aa]-(\d+)$")


def uuid7() -> UUID:
    return UUID(int=_uuid7().int)


def stable_id(kind: str, key: str) -> UUID:
    """Deterministic id for seed rows, e.g. stable_id("team", "team-rates")."""
    return uuid5(SEED_NAMESPACE, f"{kind}:{key}")


def short_token(nbytes: int = 6) -> str:
    return secrets.token_hex(nbytes)


def format_short_id(n: int) -> str:
    return f"A-{n}"


def parse_short_id(value: str) -> int | None:
    """`A-1234` -> 1234; anything else (e.g. a uuid) -> None."""
    m = _SHORT_ID_RE.match(value.strip())
    return int(m.group(1)) if m else None


def parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
