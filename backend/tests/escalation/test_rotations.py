"""Rotation slots, idempotent materialisation, on-call now/next, seeded rotations."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.ids import stable_id, uuid7
from app.escalation import oncall, rotations
from seed.seed import run_seed
from tests.delivery.helpers import user

WARSAW = ZoneInfo("Europe/Warsaw")
HOUR = timedelta(hours=1)
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def rotation(**overrides):
    return {
        "id": uuid7(),
        "team_id": uuid7(),
        "member_ids": [user("anna@demo"), user("piotr@demo")],
        "starts_at": datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
        "shift_hours": 168,
        "handover_time": None,
        "timezone": "Europe/Warsaw",
        **overrides,
    }


def test_weekly_slots_cover_the_horizon_without_gaps():
    slots = rotations.slots_between(rotation(), NOW, NOW + timedelta(days=14))
    assert [s.index for s in slots] == [0, 1, 2]
    assert slots[0].start == datetime(2026, 11, 16, tzinfo=UTC)  # the running slot is included
    assert all(a.end == b.start for a, b in itertools.pairwise(slots))
    assert slots[-1].start < NOW + timedelta(days=14) <= slots[-1].end


def test_slots_before_the_rotation_starts_are_not_generated():
    future = rotation(starts_at=NOW + timedelta(days=3))
    slots = rotations.slots_between(future, NOW, NOW + timedelta(days=14))
    assert slots[0].index == 0 and slots[0].start == NOW + timedelta(days=3)


def test_handover_time_stays_on_the_wall_clock_across_dst():
    daily = rotation(
        shift_hours=24,
        handover_time="09:00",
        starts_at=datetime(2026, 10, 23, 8, 0, tzinfo=UTC),  # DST ends on 2026-10-25 in Warsaw
    )
    slots = rotations.slots_between(
        daily, datetime(2026, 10, 23, tzinfo=UTC), datetime(2026, 10, 28, tzinfo=UTC)
    )
    assert {s.start.astimezone(WARSAW).strftime("%H:%M") for s in slots} == {"09:00"}
    assert HOUR * 25 in {s.end - s.start for s in slots}  # the day DST ends is an hour longer


def test_handover_before_start_anchors_on_the_previous_handover():
    r = rotation(
        shift_hours=168, handover_time="09:00", starts_at=datetime(2026, 11, 17, 6, 0, tzinfo=UTC)
    )  # 07:00 Warsaw, before the 09:00 handover
    (slot, *_) = rotations.slots_between(r, r["starts_at"], r["starts_at"] + timedelta(days=1))
    assert slot.start.astimezone(WARSAW) == datetime(2026, 11, 16, 9, 0, tzinfo=WARSAW)
    assert slot.start <= r["starts_at"] < slot.end


def test_parse_handover_validation():
    assert rotations.parse_handover(None) is None
    assert rotations.parse_handover("09:30").minute == 30
    with pytest.raises(ValueError):
        rotations.parse_handover("nine")


def test_non_positive_shift_hours_are_rejected():
    with pytest.raises(ValueError):
        rotations.slots_between(rotation(shift_hours=0), NOW, NOW + HOUR)


@pytest.fixture
async def fresh_team(seeded):
    team_id = uuid7()
    await seeded.execute(
        "INSERT INTO teams (id, slug, name) VALUES ($1, $2, 'Rot team')", team_id, f"rot-{team_id.hex[:8]}"
    )
    for email in ("anna@demo", "piotr@demo"):
        await seeded.execute(
            "INSERT INTO team_members (team_id, user_id) VALUES ($1, $2)", team_id, user(email)
        )
    yield team_id
    await seeded.execute("DELETE FROM shifts WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM rotations WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM team_members WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM teams WHERE id = $1", team_id)


async def add_rotation(pool, team_id, **overrides):
    values = rotation(team_id=team_id, **overrides)
    await pool.execute(
        "INSERT INTO rotations "
        "(id, team_id, name, member_ids, starts_at, shift_hours, handover_time, timezone) "
        "VALUES ($1, $2, 'r', $3, $4, $5, $6, $7)",
        values["id"],
        team_id,
        values["member_ids"],
        values["starts_at"],
        values["shift_hours"],
        values["handover_time"],
        values["timezone"],
    )
    return values


async def test_materialisation_is_idempotent_and_alternates_members(seeded, fresh_team):
    values = await add_rotation(seeded, fresh_team, shift_hours=48, starts_at=NOW - timedelta(hours=10))
    async with seeded.acquire() as conn:
        created = await rotations.materialise_all(conn, now=NOW)
        assert created == 8  # 14 days / 48 h = 7 slots plus the running one
        assert await rotations.materialise_all(conn, now=NOW) == 0  # second run changes nothing
        assert await rotations.materialise_all(conn, now=NOW + HOUR) == 0
        rows = await conn.fetch(
            "SELECT user_id, start_at, end_at, kind FROM shifts WHERE rotation_id = $1 ORDER BY start_at",
            values["id"],
        )
        later = await rotations.materialise_all(conn, now=NOW + timedelta(days=2))
    assert later == 1  # the horizon moved by one slot
    assert [r["user_id"] for r in rows[:4]] == values["member_ids"] * 2
    assert {r["kind"] for r in rows} == {"rotation"}
    assert all(a["end_at"] == b["start_at"] for a, b in itertools.pairwise(rows))


async def test_rotation_without_members_creates_nothing(seeded, fresh_team):
    values = await add_rotation(seeded, fresh_team, member_ids=[])
    async with seeded.acquire() as conn:
        assert await rotations.materialise_rotation(conn, values, now=NOW) == 0


async def test_oncall_now_and_next(seeded, fresh_team):
    await add_rotation(seeded, fresh_team, shift_hours=24, starts_at=NOW - timedelta(hours=6))
    async with seeded.acquire() as conn:
        await rotations.materialise_all(conn, now=NOW)
        now_shift, next_shift = await oncall.oncall_now_next(conn, fresh_team, NOW)
    assert now_shift["user_id"] == user("anna@demo")
    assert next_shift["user_id"] == user("piotr@demo")
    assert next_shift["start_at"] == NOW - timedelta(hours=6) + timedelta(hours=24)


async def test_override_hides_rotation_and_next_skips_to_a_change(seeded, fresh_team):
    await add_rotation(seeded, fresh_team, shift_hours=24, starts_at=NOW - timedelta(hours=6))
    async with seeded.acquire() as conn:
        await rotations.materialise_all(conn, now=NOW)
        await conn.execute(
            "INSERT INTO shifts (id, team_id, user_id, start_at, end_at, kind) "
            "VALUES ($1, $2, $3, $4, $5, 'override')",
            uuid7(),
            fresh_team,
            user("piotr@demo"),
            NOW - HOUR,
            NOW + HOUR * 3,
        )
        now_shift, next_shift = await oncall.oncall_now_next(conn, fresh_team, NOW)
        assert (now_shift["user_id"], now_shift["kind"]) == (user("piotr@demo"), "override")
        # after the override ends anna's rotation shift is on call again: that is the next change
        assert next_shift["user_id"] == user("anna@demo")
        assert next_shift["start_at"] == NOW - timedelta(hours=6)
        assert next_shift["kind"] == "rotation"


def test_next_change_without_any_shift():
    assert oncall.next_change([], NOW) == (None, None)


async def test_seeded_rotations_put_the_first_member_on_call_now(seeded):
    async with seeded.acquire() as conn:
        for slug, email in (("team-settlements", "piotr@demo"), ("team-payments", "anna@demo")):
            team_id = stable_id("team", slug)
            assert await conn.fetchval("SELECT count(*) FROM rotations WHERE team_id = $1", team_id) == 1
            assert await oncall.current_oncall(conn, team_id) == [user(email)]
            now_shift, _ = await oncall.oncall_now_next(conn, team_id)
            assert now_shift["kind"] == "rotation"
        # teams without members get no rotation
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM rotations WHERE team_id = $1", stable_id("team", "team-rates")
            )
            == 0
        )


async def test_reseeding_does_not_duplicate_rotations_or_shifts(seeded):
    before = (
        await seeded.fetchval("SELECT count(*) FROM rotations"),
        await seeded.fetchval("SELECT count(*) FROM shifts"),
    )
    await run_seed(seeded)
    after = (
        await seeded.fetchval("SELECT count(*) FROM rotations"),
        await seeded.fetchval("SELECT count(*) FROM shifts"),
    )
    assert after == before
