"""On-call precedence, effective policy merge with origins, seeded org defaults."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.ids import ORG_ID, uuid7
from app.escalation.oncall import current_oncall
from app.escalation.policies import effective_policy, scaled_seconds
from seed.seed import run_seed
from tests.delivery.helpers import domain, team, user

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
async def fresh_team(seeded):
    """A team with members anna and piotr and no shifts; removed afterwards."""
    team_id = uuid7()
    await seeded.execute(
        "INSERT INTO teams (id, slug, name) VALUES ($1, $2, 'T')", team_id, f"t-{team_id.hex[:8]}"
    )
    for email in ("anna@demo", "piotr@demo"):
        await seeded.execute(
            "INSERT INTO team_members (team_id, user_id) VALUES ($1, $2)", team_id, user(email)
        )
    yield team_id
    await seeded.execute("DELETE FROM shifts WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM team_members WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM teams WHERE id = $1", team_id)


async def add_shift(pool, team_id, email, kind, start=-HOUR, end=HOUR):
    await pool.execute(
        "INSERT INTO shifts (id, team_id, user_id, start_at, end_at, kind) VALUES ($1, $2, $3, $4, $5, $6)",
        uuid7(),
        team_id,
        user(email),
        NOW + start,
        NOW + end,
        kind,
    )


async def test_oncall_falls_back_to_all_team_members(seeded, fresh_team):
    async with seeded.acquire() as conn:
        assert set(await current_oncall(conn, fresh_team, NOW)) == {user("anna@demo"), user("piotr@demo")}


async def test_oncall_rotation_alone(seeded, fresh_team):
    await add_shift(seeded, fresh_team, "piotr@demo", "rotation")
    async with seeded.acquire() as conn:
        assert await current_oncall(conn, fresh_team, NOW) == [user("piotr@demo")]


async def test_oncall_precedence_override_over_manual_over_rotation(seeded, fresh_team):
    await add_shift(seeded, fresh_team, "piotr@demo", "rotation")
    async with seeded.acquire() as conn:
        assert await current_oncall(conn, fresh_team, NOW) == [user("piotr@demo")]
        await add_shift(seeded, fresh_team, "anna@demo", "manual")
        assert await current_oncall(conn, fresh_team, NOW) == [user("anna@demo")]
        await add_shift(seeded, fresh_team, "root.payments@demo", "override")
        assert await current_oncall(conn, fresh_team, NOW) == [user("root.payments@demo")]


async def test_oncall_ignores_shifts_outside_the_time(seeded, fresh_team):
    await add_shift(seeded, fresh_team, "anna@demo", "override", start=-3 * HOUR, end=-2 * HOUR)
    await add_shift(seeded, fresh_team, "anna@demo", "override", start=2 * HOUR, end=3 * HOUR)
    await add_shift(seeded, fresh_team, "piotr@demo", "rotation")
    async with seeded.acquire() as conn:
        assert await current_oncall(conn, fresh_team, NOW) == [user("piotr@demo")]
        assert await current_oncall(conn, fresh_team, NOW + 2 * HOUR + timedelta(minutes=1)) == [
            user("anna@demo")
        ]
        end_of_rotation = NOW + HOUR  # end_at is exclusive
        assert await current_oncall(conn, fresh_team, end_of_rotation) != [user("piotr@demo")]


async def test_oncall_defaults_to_now(seeded, fresh_team):
    now = datetime.now(UTC)
    await seeded.execute(
        "INSERT INTO shifts (id, team_id, user_id, start_at, end_at, kind) "
        "VALUES ($1, $2, $3, $4, $5, 'manual')",
        uuid7(),
        fresh_team,
        user("piotr@demo"),
        now - HOUR,
        now + HOUR,
    )
    async with seeded.acquire() as conn:
        assert await current_oncall(conn, fresh_team) == [user("piotr@demo")]


# --- policies -----------------------------------------------------------------------------------------


@pytest.fixture
async def clean_policies(seeded):
    """Remove team/domain/rule policy rows a test added (org defaults stay)."""
    yield
    await seeded.execute("DELETE FROM escalation_policies WHERE scope_type <> 'org'")


async def put_policy(
    pool, scope_type, scope_id, severity, *, levels=None, renotify=None, soft=(), digest=None
):
    await pool.execute(
        "INSERT INTO escalation_policies (scope_type, scope_id, severity, levels, renotify_seconds, "
        "soft_channels, soft_digest_minutes) VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (scope_type, scope_id, severity) DO UPDATE SET levels = $4, renotify_seconds = $5, "
        "soft_channels = $6, soft_digest_minutes = $7",
        scope_type,
        scope_id,
        severity,
        levels or [],
        renotify,
        list(soft),
        digest,
    )


async def eff(pool, rule=None, slug="team-payments", severity="critical"):
    async with pool.acquire() as conn:
        return await effective_policy(
            conn, rule=rule, team_id=team(slug), domain_id=domain("payments"), severity=severity
        )


async def test_org_defaults_are_seeded_from_the_spec_table(seeded):
    crit = await eff(seeded)
    assert [(lv.level, lv.target.value, lv.ackTimeoutSeconds) for lv in crit.levels] == [
        (1, "oncall", 300),
        (2, "domain_root", 300),
        (3, "master", None),
    ]
    assert [c.value for c in crit.levels[0].channels] == ["phone", "sms"]
    assert crit.renotify_seconds == 120 and crit.soft_channels == ["email", "ui"]
    assert set(crit.origin.values()) == {"org"} and set(crit.origin) == {
        "levels",
        "renotifySeconds",
        "softChannels",
        "softDigestMinutes",
    }
    error = await eff(seeded, severity="error")
    assert error.levels[0].ackTimeoutSeconds == 900 and error.renotify_seconds == 600
    warning = await eff(seeded, severity="warning")
    assert warning.soft_digest_minutes == 60 and warning.renotify_seconds is None
    info = await eff(seeded, severity="info")
    assert [c.value for c in info.levels[0].channels] == ["ui"]


async def test_seed_policies_does_not_overwrite_edits_and_is_idempotent(seeded):
    await seeded.execute(
        "UPDATE escalation_policies SET renotify_seconds = 77 "
        "WHERE scope_type = 'org' AND severity = 'critical'"
    )
    try:
        await run_seed(seeded)
        assert (await eff(seeded)).renotify_seconds == 77
        assert await seeded.fetchval("SELECT count(*) FROM escalation_policies WHERE scope_type = 'org'") == 4
    finally:
        await seeded.execute(
            "UPDATE escalation_policies SET renotify_seconds = 120 "
            "WHERE scope_type = 'org' AND severity = 'critical'"
        )


async def test_merge_takes_each_field_from_the_most_specific_scope(seeded, clean_policies):
    rule_id = uuid7()
    custom = [
        {
            "level": 1,
            "target": "custom",
            "channels": ["email"],
            "ackTimeoutSeconds": 45,
            "recipients": [{"kind": "user", "id": "anna@demo"}],
        }
    ]
    await put_policy(seeded, "domain", domain("payments"), "critical", renotify=300, soft=["ui"])
    await put_policy(seeded, "team", team("team-payments"), "critical", levels=custom)
    await put_policy(seeded, "rule", rule_id, "critical", digest=15)
    policy = await eff(seeded, rule={"id": str(rule_id), "routing": {}})
    assert policy.origin == {
        "levels": "team",
        "renotifySeconds": "domain",
        "softChannels": "domain",
        "softDigestMinutes": "rule",
    }
    assert policy.levels[0].ackTimeoutSeconds == 45 and policy.levels[0].recipients[0].id == "anna@demo"
    assert (
        policy.renotify_seconds == 300 and policy.soft_channels == ["ui"] and policy.soft_digest_minutes == 15
    )


async def test_other_teams_and_severities_are_not_affected(seeded, clean_policies):
    await put_policy(seeded, "team", team("team-payments"), "critical", renotify=11)
    assert (await eff(seeded)).renotify_seconds == 11
    assert (await eff(seeded, slug="team-settlements")).renotify_seconds == 120
    assert (await eff(seeded, severity="error")).renotify_seconds == 600


async def test_zero_means_explicitly_none_at_a_lower_scope(seeded, clean_policies):
    await put_policy(seeded, "team", team("team-payments"), "critical", renotify=0)
    policy = await eff(seeded)
    assert policy.renotify_seconds is None and policy.origin["renotifySeconds"] == "team"


async def test_rule_ack_timeout_override_marks_levels_origin_rule(seeded):
    rule = {
        "id": str(uuid7()),
        "routing": {},
        "escalation": {"ackTimeoutSecondsBySeverity": {"critical": 90}},
    }
    policy = await eff(seeded, rule=rule)
    assert policy.levels[0].ackTimeoutSeconds == 90 and policy.origin["levels"] == "rule"
    assert policy.levels[1].ackTimeoutSeconds == 300
    assert (await eff(seeded, rule=rule, severity="error")).origin["levels"] == "org"


async def test_without_any_rows_a_last_resort_policy_is_returned(seeded):
    await seeded.execute("DELETE FROM escalation_policies WHERE severity = 'info' AND scope_id = $1", ORG_ID)
    try:
        policy = await eff(seeded, severity="info")
        assert policy.levels[0].target.value == "oncall" and policy.soft_channels == ["email", "ui"]
    finally:
        await run_seed(seeded)


async def test_policy_level_lookup_and_wire_conversion(seeded):
    policy = await eff(seeded)
    assert policy.level_for(2).target.value == "domain_root"
    assert policy.level_for(9).level == 3 and policy.level_for(0) is None
    wire = policy.to_wire("team", str(team("team-payments")))
    dumped = wire.model_dump(mode="json", exclude_none=True)
    assert dumped["origin"]["levels"] == "org" and dumped["levels"][0]["channels"] == ["phone", "sms"]


def test_demo_fast_escalation_shrinks_ten_times_with_floors(monkeypatch):
    from app.config import get_settings

    assert scaled_seconds(300) == 300
    monkeypatch.setenv("DEMO_FAST_ESCALATION", "true")
    get_settings.cache_clear()
    try:
        assert scaled_seconds(300) == 30 and scaled_seconds(30) == 15
        assert scaled_seconds(120, renotify=True) == 60 and scaled_seconds(1800, renotify=True) == 180
    finally:
        monkeypatch.delenv("DEMO_FAST_ESCALATION")
        get_settings.cache_clear()
