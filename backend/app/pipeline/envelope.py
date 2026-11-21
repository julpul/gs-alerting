"""The internal message on `events.in` (spec 5.3) and the severity -> RabbitMQ priority map (spec 4.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.ids import uuid7

SEVERITY_PRIORITY = {"critical": 9, "error": 6, "warning": 3, "info": 0}
SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
MAX_EVENT_BYTES = 256 * 1024


def priority_for(severity: str | None) -> int:
    return SEVERITY_PRIORITY.get(severity or "", 3)


def format_ts(moment: datetime) -> str:
    """UTC ISO-8601 with millisecond precision and a `Z` suffix."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now_iso() -> str:
    return format_ts(datetime.now(UTC))


def parse_received_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_envelope(
    *,
    source_id: UUID | str,
    raw: dict[str, Any],
    priority: int,
    typed: bool = False,
    meta: dict[str, Any] | None = None,
    event_id: UUID | None = None,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Envelope exactly as spec 5.3. `_meta` stays empty for webhook events (the worker fills it)."""
    return {
        "event_id": str(event_id or uuid7()),
        "source_id": str(source_id),
        "received_at": received_at or utc_now_iso(),
        "priority": priority,
        "typed": typed,
        "raw": raw,
        "_meta": meta or {},
    }
