"""Rotation templates -> concrete shifts (spec 10.3).

A rotation is an ordered list of members that hand over every `shift_hours`. With a `handover_time` and
a whole number of days per shift the handovers stay at that wall-clock time in the rotation's timezone
(also across DST changes); otherwise slots are plain multiples of `shift_hours` from `starts_at`.
Materialisation is idempotent: `(rotation_id, start_at)` is unique.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from app.core.ids import uuid7

log = logging.getLogger(__name__)

HORIZON_DAYS = 14
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Slot:
    index: int
    start: datetime
    end: datetime


def parse_handover(value: str | None) -> time | None:
    """`09:00` -> time; None / empty -> None; garbage -> ValueError."""
    if not value:
        return None
    hh, sep, mm = value.partition(":")
    if not sep:
        raise ValueError(f"handover time must look like HH:MM, got {value!r}")
    return time(int(hh), int(mm))


def _zone(name: str | None) -> ZoneInfo:
    return ZoneInfo(name or "UTC")


def _slot_start(rotation: Mapping[str, Any], index: int) -> datetime:
    hours = rotation["shift_hours"]
    starts_at: datetime = rotation["starts_at"]
    handover = parse_handover(rotation.get("handover_time"))
    if handover is None or hours % 24:
        return starts_at + HOUR * (hours * index)
    zone = _zone(rotation.get("timezone"))
    local = starts_at.astimezone(zone)
    base = datetime.combine(local.date(), handover)
    if base.replace(tzinfo=zone) > starts_at:
        base -= timedelta(days=1)
    day = base + timedelta(days=(hours // 24) * index)
    return day.replace(tzinfo=zone).astimezone(UTC)


def slots_between(rotation: Mapping[str, Any], start: datetime, end: datetime) -> list[Slot]:
    """Slots of `rotation` that overlap `[start, end)` (a running slot is included)."""
    hours = rotation["shift_hours"]
    if hours <= 0:
        raise ValueError("shift_hours must be positive")
    elapsed_hours = (start - rotation["starts_at"]) / HOUR
    index = max(0, int(elapsed_hours // hours) - 2)
    slots: list[Slot] = []
    while True:
        slot_start = _slot_start(rotation, index)
        if slot_start >= end:
            return slots
        slot_end = _slot_start(rotation, index + 1)
        if slot_end > start:
            slots.append(Slot(index, slot_start, slot_end))
        index += 1


async def materialise_rotation(
    conn: asyncpg.Connection,
    rotation: Mapping[str, Any],
    *,
    now: datetime | None = None,
    horizon_days: int = HORIZON_DAYS,
) -> int:
    """Insert the missing shifts from now until `horizon_days` ahead; returns how many were created."""
    members = list(rotation["member_ids"])
    if not members:
        return 0
    now = now or datetime.now(UTC)
    created = 0
    for slot in slots_between(rotation, now, now + timedelta(days=horizon_days)):
        inserted = await conn.fetchval(
            "INSERT INTO shifts (id, team_id, user_id, start_at, end_at, kind, rotation_id) "
            "VALUES ($1, $2, $3, $4, $5, 'rotation', $6) "
            "ON CONFLICT (rotation_id, start_at) WHERE rotation_id IS NOT NULL DO NOTHING RETURNING id",
            uuid7(),
            rotation["team_id"],
            members[slot.index % len(members)],
            slot.start,
            slot.end,
            rotation["id"],
        )
        created += inserted is not None
    return created


async def materialise_all(
    conn: asyncpg.Connection, *, now: datetime | None = None, horizon_days: int = HORIZON_DAYS
) -> int:
    """Materialise every rotation; returns the number of shifts created."""
    total = 0
    for rotation in await conn.fetch("SELECT * FROM rotations ORDER BY id"):
        total += await materialise_rotation(conn, dict(rotation), now=now, horizon_days=horizon_days)
    if total:
        log.info("materialised %d rotation shift(s)", total)
    return total
