"""Per-user channel preferences (spec 9.2 step 3): channels per severity and working hours.

Stored as the `preferences` row the delivery routing reads: `by_severity` `{severity: [channel]}` and
`working_hours` `{from, to, timezone}`.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter

from app.api.deps import CurrentUser, Pool
from app.core.errors import AppError
from app.delivery.channel_select import parse_hhmm
from app.schemas import api as wire

router = APIRouter(prefix="/v1/preferences", tags=["schedules"])

SEVERITIES = {s.value for s in wire.Severity}


def _invalid(message: str) -> AppError:
    return AppError(422, "validation_error", message)


def _hours(prefs: wire.ChannelPreferences) -> dict[str, str]:
    hours = prefs.workingHours
    if hours is None:
        return {}
    if parse_hhmm(hours.from_) is None or parse_hhmm(hours.to) is None:
        raise _invalid("workingHours: from and to must look like HH:MM")
    try:
        ZoneInfo(hours.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise _invalid(f"workingHours.timezone: unknown timezone {hours.timezone!r}") from exc
    return {"from": str(hours.from_), "to": str(hours.to), "timezone": hours.timezone or "UTC"}


def _out(by_severity: dict[str, Any], working_hours: dict[str, Any]) -> dict[str, Any]:
    return {"bySeverity": by_severity or {}, "workingHours": working_hours or None}


@router.get("/me", response_model=wire.ChannelPreferences)
async def get_preferences(user: CurrentUser, pool: Pool) -> dict[str, Any]:
    row = await pool.fetchrow(
        "SELECT by_severity, working_hours FROM preferences WHERE user_id = $1", user.id
    )
    return _out(row["by_severity"], row["working_hours"]) if row else _out({}, {})


@router.put("/me", response_model=wire.ChannelPreferences)
async def put_preferences(body: wire.ChannelPreferences, user: CurrentUser, pool: Pool) -> dict[str, Any]:
    by_severity = {
        sev: [c.value for c in dict.fromkeys(channels)] for sev, channels in (body.bySeverity or {}).items()
    }
    unknown = next((s for s in by_severity if s not in SEVERITIES), None)
    if unknown is not None:
        raise _invalid(f"bySeverity.{unknown}: unknown severity")
    hours = _hours(body)
    await pool.execute(
        "INSERT INTO preferences (user_id, by_severity, working_hours) VALUES ($1, $2, $3) "
        "ON CONFLICT (user_id) DO UPDATE SET by_severity = EXCLUDED.by_severity, "
        "working_hours = EXCLUDED.working_hours",
        user.id,
        by_severity,
        hours,
    )
    return _out(by_severity, hours)
