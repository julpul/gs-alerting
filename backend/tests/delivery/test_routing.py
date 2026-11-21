"""Recipient resolution and delivery planning against the seeded organisation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.ids import uuid7
from app.core.security import encrypt_secret
from app.delivery.recipients import resolve_spec, resolve_specs
from app.delivery.ruleview import RecipientSpec
from tests.delivery.helpers import domain, make_alert, plan, rule_def, team, user

NOON = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)  # 12:00 Warsaw
LATE = datetime(2026, 11, 20, 21, 30, tzinfo=UTC)  # 22:30 Warsaw
SETTLEMENTS = {"kind": "oncall", "id": "team-settlements"}
MANAGER_SOFT = {"kind": "role", "id": "team-settlements:informed"}
_minute = iter(range(1, 10_000))


@pytest.fixture(autouse=True)
async def settle_outbox(seeded):
    """Rows written by planning tests must never be picked up by workers or relays of other tests."""
    yield
    await seeded.execute("UPDATE outbox SET published_at = now() WHERE published_at IS NULL")


def unique_minute() -> datetime:
    return datetime(2040, 1, 1, tzinfo=UTC) + timedelta(minutes=10 * next(_minute))


def summary(rows):
    return sorted((r["channel"], r["kind"], r["recipient_user_id"]) for r in rows)


async def resolve(pool, kind, ref, slug="team-settlements"):
    async with pool.acquire() as conn:
        people = await resolve_spec(
            conn, RecipientSpec(kind, ref), team_id=team(slug), domain_id=domain("settlements"), at=NOON
        )
    return {p.id for p in people}


async def test_resolve_oncall_by_slug_uuid_and_default_team(seeded):
    assert await resolve(seeded, "oncall", "team-settlements") == {user("piotr@demo")}
    assert await resolve(seeded, "oncall", str(team("team-settlements"))) == {user("piotr@demo")}
    assert await resolve(seeded, "oncall", "") == {user("piotr@demo")}  # alert's own team
    assert await resolve(seeded, "oncall", "no-such-team") == set()
    assert await resolve(seeded, "oncall", "team-rates") == set()  # team without members


async def test_resolve_user_team_and_roles(seeded):
    assert await resolve(seeded, "user", "anna@demo") == {user("anna@demo")}
    assert await resolve(seeded, "user", "ANNA@demo") == {user("anna@demo")}
    assert await resolve(seeded, "user", str(user("piotr@demo"))) == {user("piotr@demo")}
    assert await resolve(seeded, "user", "nobody@demo") == set()
    assert await resolve(seeded, "team", "team-payments") == {user("anna@demo")}
    assert await resolve(seeded, "role", "team-rates:informed") == {user("manager@demo")}
    assert await resolve(seeded, "role", "team-payments:member") == {user("anna@demo")}
    assert await resolve(seeded, "role", "informed") == {user("manager@demo")}  # alert's own team
    assert await resolve(seeded, "role", "team-rates:nope") == set()


async def test_resolve_domain_root_and_master(seeded):
    assert await resolve(seeded, "domain_root", "") == {user("root.settlements@demo")}
    assert await resolve(seeded, "domain_root", "payments") == {user("root.payments@demo")}
    assert await resolve(seeded, "master", "") == {user("master@demo")}
    assert await resolve(seeded, "pager", "x") == set()


async def test_resolve_specs_dedupes_in_order(seeded):
    specs = [
        RecipientSpec("user", "piotr@demo"),
        RecipientSpec("oncall", "team-settlements"),
        RecipientSpec("user", "anna@demo"),
    ]
    async with seeded.acquire() as conn:
        people = await resolve_specs(
            conn, specs, team_id=team("team-settlements"), domain_id=domain("settlements")
        )
    assert [p.id for p in people] == [user("piotr@demo"), user("anna@demo")]


async def test_critical_hard_plan_phone_sms_plus_soft_email_and_ui(seeded):
    alert = await make_alert(seeded, "team-settlements", severity="critical")
    rule = rule_def(hard=[SETTLEMENTS], soft=[MANAGER_SOFT], channels={"critical": ["phone", "sms"]})
    rows = await plan(seeded, alert, rule, reason="created", now=NOON)
    piotr, manager = user("piotr@demo"), user("manager@demo")
    assert summary(rows) == sorted(
        [("phone", "hard", piotr), ("sms", "hard", piotr), ("email", "soft", manager), ("ui", "hard", None)]
    )
    assert all(r["status"] == "pending" and r["outbox_id"] for r in rows)
    sms = next(r for r in rows if r["channel"] == "sms")
    assert sms["idempotency_key"] == f"{alert['id']}:1:{piotr}:sms"
    assert next(r for r in rows if r["channel"] == "ui")["idempotency_key"] == f"{alert['id']}:1:team:ui"

    stored = await seeded.fetchrow("SELECT * FROM deliveries WHERE id = $1", sms["id"])
    assert stored["priority"] == 9 and stored["notification_seq"] == 1
    assert stored["event_received_at"] == alert["first_event_received_at"]
    assert stored["recipient_address"] == "+48500100201"
    box = await seeded.fetchrow("SELECT * FROM outbox WHERE id = $1", sms["outbox_id"])
    assert (box["topic"], box["routing_key"], box["priority"]) == ("deliveries", "sms", 9)
    assert box["payload"] == {"deliveryId": str(sms["id"])}

    fresh = await seeded.fetchrow(
        "SELECT ack_deadline, next_renotify_at FROM alerts WHERE id = $1", alert["id"]
    )
    assert fresh["ack_deadline"] == NOON + timedelta(seconds=300)
    assert fresh["next_renotify_at"] == NOON + timedelta(seconds=120)


async def test_demo_fast_escalation_shrinks_deadlines(seeded, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("DEMO_FAST_ESCALATION", "true")
    get_settings.cache_clear()
    try:
        alert = await make_alert(seeded, severity="critical")
        await plan(seeded, alert, rule_def(hard=[SETTLEMENTS]), reason="created", now=NOON)
    finally:
        monkeypatch.delenv("DEMO_FAST_ESCALATION")
        get_settings.cache_clear()
    fresh = await seeded.fetchrow(
        "SELECT ack_deadline, next_renotify_at FROM alerts WHERE id = $1", alert["id"]
    )
    assert fresh["ack_deadline"] == NOON + timedelta(seconds=5)
    assert fresh["next_renotify_at"] == NOON + timedelta(seconds=2)


async def test_anna_sms_inside_window_email_outside(seeded):
    window = {"start": "08:00", "end": "20:00", "timezone": "Europe/Warsaw", "outsideChannels": ["email"]}
    rule = rule_def(
        hard=[{"kind": "user", "id": "anna@demo"}], channels={"critical": ["sms"]}, time_window=window
    )
    alert = await make_alert(seeded, "team-payments", severity="critical")
    inside = await plan(seeded, alert, rule, reason="created", now=NOON)
    assert [r["channel"] for r in inside if r["recipient_user_id"]] == ["sms"]
    outside = await plan(seeded, alert, rule, reason="renotify", now=LATE)
    assert [(r["channel"], r["notification_seq"]) for r in outside] == [("email", 2)]


async def test_user_preferences_apply_when_rule_has_no_mapping(seeded):
    await seeded.execute(
        "INSERT INTO preferences (user_id, by_severity, working_hours) VALUES ($1, $2, '{}') "
        "ON CONFLICT (user_id) DO UPDATE SET by_severity = $2",
        user("anna@demo"),
        {"critical": ["email"]},
    )
    try:
        alert = await make_alert(seeded, "team-payments", severity="critical")
        rows = await plan(
            seeded, alert, rule_def(hard=[{"kind": "user", "id": "anna@demo"}]), reason="created", now=NOON
        )
        assert [r["channel"] for r in rows if r["recipient_user_id"]] == ["email"]
    finally:
        await seeded.execute("DELETE FROM preferences WHERE user_id = $1", user("anna@demo"))


async def test_policy_level_channels_when_nothing_else_applies(seeded):
    alert = await make_alert(seeded, severity="error")
    rows = await plan(seeded, alert, rule_def(hard=[SETTLEMENTS]), reason="created", now=NOON)
    assert summary(rows) == sorted([("sms", "hard", user("piotr@demo")), ("ui", "hard", None)])
    assert next(r for r in rows if r["channel"] == "sms")["outbox_id"]
    fresh = await seeded.fetchrow(
        "SELECT ack_deadline, next_renotify_at FROM alerts WHERE id = $1", alert["id"]
    )
    assert fresh["ack_deadline"] == NOON + timedelta(seconds=900)
    assert fresh["next_renotify_at"] == NOON + timedelta(seconds=600)
    assert [
        r["priority"]
        for r in await seeded.fetch("SELECT priority FROM deliveries WHERE alert_id = $1", alert["id"])
    ] == [6, 6]


async def test_rates_rule_without_oncall_members_is_soft_only_without_deadline(seeded):
    rule = rule_def(
        hard=[{"kind": "oncall", "id": "team-rates"}],
        soft=[{"kind": "role", "id": "team-rates:informed"}],
        channels={"error": ["email", "ui"]},
    )
    alert = await make_alert(seeded, "team-rates", severity="error")
    rows = await plan(seeded, alert, rule, reason="created", now=NOON)
    assert summary(rows) == sorted([("email", "soft", user("manager@demo")), ("ui", "soft", None)])
    fresh = await seeded.fetchrow(
        "SELECT ack_deadline, next_renotify_at FROM alerts WHERE id = $1", alert["id"]
    )
    assert fresh["ack_deadline"] is None and fresh["next_renotify_at"] is None


async def test_unstaffed_hard_list_falls_back_to_domain_roots(seeded):
    alert = await make_alert(seeded, "team-security", severity="error")
    rows = await plan(
        seeded, alert, rule_def(hard=[{"kind": "oncall", "id": "team-security"}]), reason="created", now=NOON
    )
    assert [r["recipient_user_id"] for r in rows if r["channel"] == "sms"] == [user("root.security@demo")]


async def test_informational_rule_has_no_deadline_and_no_escalation(seeded):
    alert = await make_alert(seeded, "team-pricing", severity="warning")
    rule = rule_def(soft=[], channels={"warning": ["ui"]})
    rows = await plan(seeded, alert, rule, reason="created", now=NOON)
    assert summary(rows) == [("ui", "soft", None)]
    fresh = await seeded.fetchrow(
        "SELECT ack_deadline, next_renotify_at FROM alerts WHERE id = $1", alert["id"]
    )
    assert fresh["ack_deadline"] is None and fresh["next_renotify_at"] is None
    assert await plan(seeded, alert, None, reason="created", now=NOON)  # no rule at all: still the feed
    assert (await plan(seeded, alert, None, reason="escalated", level=2, now=NOON))[0]["channel"] == "ui"


async def test_soft_list_excludes_people_on_the_hard_list(seeded):
    alert = await make_alert(seeded, "team-payments", severity="error")
    rule = rule_def(
        hard=[{"kind": "user", "id": "anna@demo"}],
        soft=[{"kind": "team", "id": "team-payments"}, {"kind": "role", "id": "team-payments:informed"}],
    )
    rows = await plan(seeded, alert, rule, reason="created", now=NOON)
    soft = [r for r in rows if r["kind"] == "soft" and r["recipient_user_id"]]
    assert {r["recipient_user_id"] for r in soft} == {user("manager@demo")}
    assert user("anna@demo") in {r["recipient_user_id"] for r in rows if r["kind"] == "hard"}
    assert not any(r["recipient_user_id"] == user("anna@demo") and r["kind"] == "soft" for r in rows)


async def test_soft_warning_goes_to_digest_but_ui_is_immediate(seeded):
    alert = await make_alert(seeded, "team-payments", severity="warning")
    rule = rule_def(soft=[{"kind": "role", "id": "team-payments:informed"}])
    rows = await plan(seeded, alert, rule, reason="created", now=NOON)
    email = next(r for r in rows if r["channel"] == "email")
    ui = next(r for r in rows if r["channel"] == "ui")
    assert email["status"] == "suppressed_digest" and email["outbox_id"] is None
    assert ui["status"] == "pending" and ui["outbox_id"]
    entry = await seeded.fetchrow("SELECT * FROM timeline WHERE alert_id = $1", alert["id"])
    assert entry["type"] == "notified" and "held back" in entry["text"]


async def test_throttle_suppresses_beyond_max_per_recipient_per_minute(seeded):
    rule = rule_def(hard=[SETTLEMENTS], channels={"critical": ["sms"]}, max_per_minute=2)
    minute = unique_minute()
    statuses = []
    for _ in range(4):
        alert = await make_alert(seeded, severity="critical")
        rows = await plan(seeded, alert, rule, reason="created", now=minute)
        sms = next(r for r in rows if r["channel"] == "sms")
        statuses.append((sms["status"], sms["outbox_id"] is not None))
        if sms["status"] == "suppressed_digest":
            stored = await seeded.fetchval("SELECT status FROM deliveries WHERE id = $1", sms["id"])
            assert stored == "suppressed_digest"
            assert (
                await seeded.fetchval(
                    "SELECT count(*) FROM outbox WHERE payload->>'deliveryId' = $1", str(sms["id"])
                )
                == 0
            )
    assert statuses == [
        ("pending", True),
        ("pending", True),
        ("suppressed_digest", False),
        ("suppressed_digest", False),
    ]
    next_minute = await plan(
        seeded, await make_alert(seeded), rule, reason="created", now=minute + timedelta(minutes=1)
    )
    assert next(r for r in next_minute if r["channel"] == "sms")["status"] == "pending"


async def test_default_throttle_is_ten_per_minute(seeded):
    rule = rule_def(hard=[SETTLEMENTS], channels={"critical": ["sms"]})
    minute = unique_minute()
    results = [
        next(
            r
            for r in await plan(seeded, await make_alert(seeded), rule, reason="created", now=minute)
            if r["channel"] == "sms"
        )["status"]
        for _ in range(11)
    ]
    assert results.count("pending") == 10 and results[-1] == "suppressed_digest"


async def test_redis_failure_never_blocks_notifications(seeded, monkeypatch):
    def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.delivery.routing.get_redis", broken)
    rows = await plan(
        seeded,
        await make_alert(seeded),
        rule_def(hard=[SETTLEMENTS], channels={"critical": ["sms"]}),
        reason="created",
        now=NOON,
    )
    assert next(r for r in rows if r["channel"] == "sms")["status"] == "pending"


async def test_escalation_levels_pick_domain_root_then_master_with_level_deadlines(seeded):
    alert = await make_alert(seeded, severity="critical")
    rule = rule_def(hard=[SETTLEMENTS], channels={})
    level2 = await plan(seeded, alert, rule, reason="escalated", level=2, now=NOON)
    assert summary([r for r in level2 if r["recipient_user_id"]]) == sorted(
        [("phone", "hard", user("root.settlements@demo")), ("sms", "hard", user("root.settlements@demo"))]
    )
    assert [r["notification_seq"] for r in level2] == [1, 1, 1]
    fresh = await seeded.fetchrow("SELECT ack_deadline FROM alerts WHERE id = $1", alert["id"])
    assert fresh["ack_deadline"] == NOON + timedelta(seconds=300)
    level3 = await plan(seeded, alert, rule, reason="escalated", level=3, now=NOON)
    assert {r["recipient_user_id"] for r in level3 if r["recipient_user_id"]} == {user("master@demo")}
    assert {r["notification_seq"] for r in level3} == {2}
    fresh = await seeded.fetchrow("SELECT ack_deadline FROM alerts WHERE id = $1", alert["id"])
    assert fresh["ack_deadline"] is None  # last level: no further escalation


async def test_rule_escalation_path_overrides_the_pyramid(seeded):
    alert = await make_alert(seeded, severity="critical")
    rule = rule_def(hard=[SETTLEMENTS], path=[{"kind": "user", "id": "anna@demo"}])
    rows = await plan(seeded, alert, rule, reason="escalated", level=2, now=NOON)
    assert {r["recipient_user_id"] for r in rows if r["recipient_user_id"]} == {user("anna@demo")}


async def test_custom_level_recipients_from_policy(seeded):
    scope_id = team("team-security")
    levels = [
        {"level": 1, "target": "oncall", "channels": ["sms"], "ackTimeoutSeconds": 60},
        {
            "level": 2,
            "target": "custom",
            "channels": ["email"],
            "ackTimeoutSeconds": None,
            "recipients": [{"kind": "user", "id": "anna@demo"}],
        },
    ]
    await seeded.execute(
        "INSERT INTO escalation_policies (scope_type, scope_id, severity, levels) "
        "VALUES ('team', $1, 'critical', $2)",
        scope_id,
        levels,
    )
    try:
        alert = await make_alert(seeded, "team-security", severity="critical")
        rows = await plan(
            seeded,
            alert,
            rule_def(hard=[{"kind": "oncall", "id": "team-security"}]),
            reason="escalated",
            level=2,
            now=NOON,
        )
        assert summary([r for r in rows if r["recipient_user_id"]]) == [("email", "hard", user("anna@demo"))]
    finally:
        await seeded.execute("DELETE FROM escalation_policies WHERE scope_type = 'team'")


async def test_renotify_reaches_hard_recipients_only_and_rearms_timer(seeded):
    alert = await make_alert(seeded, severity="critical")
    rule = rule_def(hard=[SETTLEMENTS], soft=[MANAGER_SOFT], channels={"critical": ["sms"]})
    await plan(seeded, alert, rule, reason="created", now=NOON)
    later = NOON + timedelta(minutes=2)
    rows = await plan(seeded, alert, rule, reason="renotify", now=later)
    assert summary(rows) == [("sms", "hard", user("piotr@demo"))] and rows[0]["notification_seq"] == 2
    fresh = await seeded.fetchrow(
        "SELECT ack_deadline, next_renotify_at FROM alerts WHERE id = $1", alert["id"]
    )
    assert fresh["next_renotify_at"] == later + timedelta(seconds=120)
    assert fresh["ack_deadline"] == NOON + timedelta(seconds=300)  # untouched


async def test_resolved_is_a_soft_update_to_everyone_without_sms_or_phone(seeded):
    alert = await make_alert(seeded, severity="critical", status="resolved")
    rule = rule_def(hard=[SETTLEMENTS], soft=[MANAGER_SOFT], channels={"critical": ["phone", "sms"]})
    rows = await plan(seeded, alert, rule, reason="resolved", now=NOON)
    assert {r["channel"] for r in rows} <= {"email", "ui"}
    assert {r["recipient_user_id"] for r in rows if r["recipient_user_id"]} == {
        user("piotr@demo"),
        user("manager@demo"),
    }
    assert all(r["kind"] == "soft" for r in rows)
    fresh = await seeded.fetchrow("SELECT ack_deadline FROM alerts WHERE id = $1", alert["id"])
    assert fresh["ack_deadline"] is None


async def test_severity_up_and_reopened_notify_like_created_without_arming_timers(seeded):
    alert = await make_alert(seeded, severity="critical")
    rule = rule_def(hard=[SETTLEMENTS], channels={"critical": ["sms"]})
    rows = await plan(seeded, alert, rule, reason="severity_up", now=NOON)
    assert ("sms", "hard", user("piotr@demo")) in summary(rows)
    fresh = await seeded.fetchrow("SELECT ack_deadline FROM alerts WHERE id = $1", alert["id"])
    assert fresh["ack_deadline"] is None
    assert any(r["channel"] == "sms" for r in await plan(seeded, alert, rule, reason="reopened", now=NOON))


async def test_people_without_phone_degrade_phone_to_sms_to_email(seeded):
    uid = uuid7()
    await seeded.execute(
        "INSERT INTO users (id, email, display_name, password_hash) "
        "VALUES ($1, 'nophone@demo', 'No Phone', 'x')",
        uid,
    )
    try:
        alert = await make_alert(seeded, severity="critical")
        rule = rule_def(hard=[{"kind": "user", "id": str(uid)}], channels={"critical": ["phone", "sms"]})
        rows = [
            r for r in await plan(seeded, alert, rule, reason="created", now=NOON) if r["recipient_user_id"]
        ]
        assert [(r["channel"], r["recipient_address"]) for r in rows] == [("email", "nophone@demo")]
    finally:
        await seeded.execute("DELETE FROM users WHERE id = $1", uid)


async def test_existing_idempotency_key_is_not_inserted_twice(seeded):
    alert = await make_alert(seeded, severity="critical")
    rule = rule_def(hard=[SETTLEMENTS], channels={"critical": ["sms"]})
    clash = f"{alert['id']}:1:{user('piotr@demo')}:sms"
    await seeded.execute(
        "INSERT INTO deliveries (id, alert_id, channel, kind, notification_seq, idempotency_key, status) "
        "VALUES ($1, $2, 'sms', 'hard', 0, $3, 'sent')",
        uuid7(),
        alert["id"],
        clash,
    )
    rows = await plan(seeded, alert, rule, reason="created", now=NOON)
    assert [r["channel"] for r in rows] == ["ui"]  # the sms key already existed
    assert await seeded.fetchval("SELECT count(*) FROM deliveries WHERE idempotency_key = $1", clash) == 1


async def test_webhook_channel_targets_team_subscriptions_with_secret(seeded):
    sub_id = uuid7()
    await seeded.execute(
        "INSERT INTO webhook_subscriptions (id, team_id, url, secret_encrypted, event_types) "
        "VALUES ($1, $2, $3, $4, '{}')",
        sub_id,
        team("team-settlements"),
        "http://hooks.test/in",
        encrypt_secret("s3cret"),
    )
    try:
        alert = await make_alert(seeded, severity="critical")
        rule = rule_def(hard=[SETTLEMENTS], channels={"critical": ["webhook"]})
        rows = await plan(seeded, alert, rule, reason="created", now=NOON)
        hook = next(r for r in rows if r["channel"] == "webhook")
        assert hook["recipient_address"] == "http://hooks.test/in" and hook["recipient_user_id"] is None
        assert hook["idempotency_key"].startswith(f"{alert['id']}:1:hook-")
    finally:
        await seeded.execute("DELETE FROM webhook_subscriptions WHERE id = $1", sub_id)
