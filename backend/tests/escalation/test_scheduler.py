"""Scheduler jobs: escalation chain, ack, renotify, concurrency, approval expiry, the 1 s loop."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime, timedelta

from app.core.ids import uuid7
from app.escalation import jobs
from app.escalation.scheduler import Scheduler, digest_interval
from app.repositories import alert_actions as actions
from app.schemas.internal import User
from tests.escalation.helpers import (
    SseCollector,
    alert_row,
    make_alert,
    make_due,
    make_rule,
    team,
    user,
    wait_for,
)

SETTLEMENTS = team("team-settlements")


async def deliveries(pool, alert_id, *, ui=False, **where):
    """Deliveries of an alert (without the team-wide `ui` ones unless `ui`), filtered by column values."""
    rows = await pool.fetch(
        "SELECT d.*, u.email FROM deliveries d LEFT JOIN users u ON u.id = d.recipient_user_id "
        "WHERE d.alert_id = $1 ORDER BY d.created_at, d.id",
        alert_id,
    )
    return [r for r in rows if (ui or r["channel"] != "ui") and all(r[k] == v for k, v in where.items())]


async def timeline_texts(pool, alert_id, type_):
    rows = await pool.fetch("SELECT text FROM timeline WHERE alert_id = $1 AND type = $2", alert_id, type_)
    return [r["text"] for r in rows]


async def piotr(pool) -> User:
    return User(**dict(await pool.fetchrow("SELECT * FROM users WHERE id = $1", user("piotr@demo"))))


async def test_fast_mode_critical_escalates_oncall_to_domain_root_to_master(seeded, fast_mode, fake_rabbit):
    rule_id = await make_rule(seeded)
    alert = await make_alert(seeded, rule_id)
    assert alert["escalation_level"] == 1
    first = await deliveries(seeded, alert["id"], kind="hard")
    assert {d["recipient_user_id"] for d in first} == {user("piotr@demo")}
    assert {d["channel"] for d in first} == {"phone", "sms"}
    remaining = (alert["ack_deadline"] - datetime.now(UTC)).total_seconds()
    assert 0 < remaining <= 30  # 5 minutes became 30 seconds (fast mode divides by 10)

    async with SseCollector(SETTLEMENTS) as sse:
        await make_due(seeded, alert["id"])
        assert await jobs.escalate_due(seeded, fake_rabbit) == 1
        level2 = await alert_row(seeded, alert["id"])
        assert (level2["escalation_level"], level2["status"]) == (2, "escalated")
        assert 0 < (level2["ack_deadline"] - datetime.now(UTC)).total_seconds() <= 30
        hard = await deliveries(seeded, alert["id"], kind="hard", notification_seq=2)
        assert {d["recipient_user_id"] for d in hard} == {user("root.settlements@demo")}
        assert {d["channel"] for d in hard} == {"phone", "sms"}
        assert fake_rabbit.published  # outbox rows were published after the commit

        await make_due(seeded, alert["id"])
        assert await jobs.escalate_due(seeded, fake_rabbit) == 1
        level3 = await alert_row(seeded, alert["id"])
        assert (level3["escalation_level"], level3["status"]) == (3, "escalated")
        assert level3["ack_deadline"] is None  # last level: nothing left to escalate to
        assert level3["next_renotify_at"] is not None
        master = await deliveries(seeded, alert["id"], kind="hard", notification_seq=3)
        assert {d["recipient_user_id"] for d in master} == {user("master@demo")}

        events = await sse.of_type("alert.escalated")
    assert [e["data"]["level"] for e in events][:2] == [2, 3]
    assert events[0]["data"]["alertId"] == str(alert["id"])
    assert events[0]["data"]["status"] == "escalated"

    texts = await timeline_texts(seeded, alert["id"], "escalated")
    assert texts == [
        "Escalated to Domain Root (no ack in 5 min)",
        "Escalated to Master (no ack in 5 min)",
    ]
    assert await jobs.escalate_due(seeded, fake_rabbit) == 0  # nothing more to do
    await make_due(seeded, alert["id"])
    assert (await alert_row(seeded, alert["id"]))["escalation_level"] == 3


async def test_ack_stops_escalation(seeded, fast_mode, fake_rabbit):
    alert = await make_alert(seeded, await make_rule(seeded))
    async with seeded.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM alerts WHERE id = $1", alert["id"])
        assert await actions.ack_alert(conn, row, await piotr(seeded))
    acked = await alert_row(seeded, alert["id"])
    assert acked["ack_deadline"] is None and acked["next_renotify_at"] is None
    await make_due(seeded, alert["id"])
    await seeded.execute("UPDATE alerts SET ack_deadline = now() - interval '1 s' WHERE id = $1", alert["id"])
    assert await jobs.escalate_due(seeded, fake_rabbit) == 0  # acknowledged alerts are never escalated
    assert (await alert_row(seeded, alert["id"]))["escalation_level"] == 1


async def test_resolved_alerts_are_not_escalated_or_renotified(seeded, fast_mode, fake_rabbit):
    alert = await make_alert(seeded, await make_rule(seeded), due=True)
    await seeded.execute("UPDATE alerts SET status = 'resolved' WHERE id = $1", alert["id"])
    assert await jobs.escalate_due(seeded, fake_rabbit) == 0
    assert await jobs.renotify_due(seeded, fake_rabbit) == 0


async def test_renotify_repeats_hard_notification_until_ack(seeded, fast_mode, fake_rabbit):
    alert = await make_alert(seeded, await make_rule(seeded))
    armed = (await alert_row(seeded, alert["id"]))["next_renotify_at"]
    assert 0 < (armed - datetime.now(UTC)).total_seconds() <= 60  # reminders stay at least a minute apart
    before = len(await deliveries(seeded, alert["id"]))

    await make_due(seeded, alert["id"])
    # only the renotify timer is due here; the ack deadline must not move the alert up a level
    await seeded.execute(
        "UPDATE alerts SET ack_deadline = now() + interval '1 hour' WHERE id = $1", alert["id"]
    )
    assert await jobs.renotify_due(seeded, fake_rabbit) == 1

    renotified = await deliveries(seeded, alert["id"], notification_seq=2)
    assert {d["recipient_user_id"] for d in renotified} == {user("piotr@demo")}
    assert {d["channel"] for d in renotified} == {"phone", "sms"}
    assert len(await deliveries(seeded, alert["id"])) == before + 2
    after = await alert_row(seeded, alert["id"])
    assert after["next_renotify_at"] > datetime.now(UTC)  # re-armed for the next round
    assert after["escalation_level"] == 1
    assert await timeline_texts(seeded, alert["id"], "notified") == ["Renotified (level 1, still no ack)"]
    assert await jobs.renotify_due(seeded, fake_rabbit) == 0


async def test_alert_without_hard_recipients_does_not_loop(seeded, fake_rabbit):
    """A due alert whose rule has no hard list is advanced once and its timers cleared."""
    alert = await make_alert(seeded, await make_rule(seeded, hard=[]), plan=False)
    await seeded.execute(
        "UPDATE alerts SET ack_deadline = now() - interval '1 s', next_renotify_at = now() - interval '1 s' "
        "WHERE id = $1",
        alert["id"],
    )
    assert await jobs.escalate_due(seeded, fake_rabbit) == 1
    assert await jobs.renotify_due(seeded, fake_rabbit) == 1
    after = await alert_row(seeded, alert["id"])
    assert after["ack_deadline"] is None and after["next_renotify_at"] is None
    assert await jobs.escalate_due(seeded, fake_rabbit) == 0
    assert await jobs.renotify_due(seeded, fake_rabbit) == 0


async def test_rule_without_further_levels_stops_at_last_level(seeded, fake_rabbit):
    """`info` has a single policy level: a stale deadline is cleared without escalating."""
    alert = await make_alert(seeded, await make_rule(seeded), severity="info", plan=False)
    await seeded.execute("UPDATE alerts SET ack_deadline = now() - interval '1 s' WHERE id = $1", alert["id"])
    assert await jobs.escalate_due(seeded, fake_rabbit) == 0
    after = await alert_row(seeded, alert["id"])
    assert after["ack_deadline"] is None and after["escalation_level"] == 1


async def test_rule_escalation_path_names_targets(seeded, fast_mode, fake_rabbit):
    rule_id = await make_rule(
        seeded,
        escalation={"path": [{"kind": "user", "id": "anna@demo"}, {"kind": "user", "id": "master@demo"}]},
    )
    alert = await make_alert(seeded, rule_id, due=True)
    assert await jobs.escalate_due(seeded, fake_rabbit) == 1
    hard = await deliveries(seeded, alert["id"], kind="hard", notification_seq=2)
    assert {d["recipient_user_id"] for d in hard} == {user("anna@demo")}


async def test_failing_escalation_is_deferred_not_looped(seeded, fake_rabbit, monkeypatch):
    alert = await make_alert(seeded, await make_rule(seeded), due=True)

    async def boom(*args, **kwargs):
        raise RuntimeError("planning broke")

    monkeypatch.setattr(jobs, "plan_deliveries", boom)
    assert await jobs.escalate_due(seeded, fake_rabbit) == 0
    after = await alert_row(seeded, alert["id"])
    assert after["escalation_level"] == 1  # the savepoint rolled the half-done escalation back
    assert after["ack_deadline"] > datetime.now(UTC)  # retried in a few seconds, not every tick


async def test_two_schedulers_never_double_escalate(seeded, fast_mode, fake_rabbit):
    rule_id = await make_rule(seeded)
    alerts = [await make_alert(seeded, rule_id, due=True) for _ in range(12)]
    ids = [a["id"] for a in alerts]

    results = await asyncio.gather(*(jobs.escalate_due(seeded, fake_rabbit, limit=5) for _ in range(4)))
    # drain whatever the limited batches left over, again from competing schedulers
    results += await asyncio.gather(*(jobs.escalate_due(seeded, fake_rabbit) for _ in range(3)))
    assert sum(results) == 12

    for alert_id in ids:
        row = await alert_row(seeded, alert_id)
        assert row["escalation_level"] == 2
        assert len(await timeline_texts(seeded, alert_id, "escalated")) == 1
        seqs = {d["notification_seq"] for d in await deliveries(seeded, alert_id, kind="hard")}
        assert seqs == {1, 2}


async def test_two_scheduler_ticks_race_on_renotify(seeded, fast_mode, fake_rabbit):
    rule_id = await make_rule(seeded)
    alerts = [await make_alert(seeded, rule_id) for _ in range(6)]
    for a in alerts:
        await make_due(seeded, a["id"])
        await seeded.execute(
            "UPDATE alerts SET ack_deadline = now() + interval '1 hour' WHERE id = $1", a["id"]
        )
    results = await asyncio.gather(*(jobs.renotify_due(seeded, fake_rabbit) for _ in range(3)))
    assert sum(results) == 6
    for a in alerts:
        assert len(await timeline_texts(seeded, a["id"], "notified")) == 1


async def test_expire_approvals_marks_timeline_and_publishes(seeded, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.runbooks.approvals", None)  # force the built-in fallback
    alert = await make_alert(seeded, None, plan=False)

    async def approval(expires_in: timedelta, status="pending"):
        approval_id = uuid7()
        await seeded.execute(
            "INSERT INTO approvals (id, status, origin, alert_id, action, parameters, risk, "
            "approver_user_ids, expires_at) "
            "VALUES ($1, $2, 'runbook_step', $3, 'restart_service', '{}', 'high', $4, $5)",
            approval_id,
            status,
            alert["id"],
            [user("piotr@demo")],
            datetime.now(UTC) + expires_in,
        )
        return approval_id

    stale = await approval(-timedelta(seconds=5))
    fresh = await approval(timedelta(minutes=5))
    decided = await approval(-timedelta(seconds=5), status="approved")
    async with SseCollector(SETTLEMENTS) as sse:
        assert await jobs.expire_approvals(seeded) == 1
        events = await sse.of_type("approval.decided")
    assert events[0]["data"]["approvalId"] == str(stale)
    assert events[0]["data"]["status"] == "expired"
    status = {r["id"]: r["status"] for r in await seeded.fetch("SELECT id, status FROM approvals")}
    assert (status[stale], status[fresh], status[decided]) == ("expired", "pending", "approved")
    (text,) = await timeline_texts(seeded, alert["id"], "approval_decided")
    assert "expired" in text and "restart_service" in text
    assert await jobs.expire_approvals(seeded) == 0


async def test_scheduler_loop_escalates_in_real_time(seeded, rabbit, fast_mode, monkeypatch):
    """The 1 s loop in fast mode, with team policy timeouts of one second per level."""
    from app.escalation import policies

    monkeypatch.setattr(policies, "FAST_FACTOR", 60)
    monkeypatch.setattr(policies, "FAST_FLOOR_SECONDS", 1)
    levels = [
        {"level": 1, "target": "oncall", "channels": ["sms"], "ackTimeoutSeconds": 60},
        {"level": 2, "target": "domain_root", "channels": ["sms"], "ackTimeoutSeconds": 60},
        {"level": 3, "target": "master", "channels": ["sms"], "ackTimeoutSeconds": None},
    ]
    await seeded.execute(
        "INSERT INTO escalation_policies (scope_type, scope_id, severity, levels, renotify_seconds) "
        "VALUES ('team', $1, 'critical', $2, 120)",
        SETTLEMENTS,
        levels,
    )
    stop = asyncio.Event()
    scheduler = Scheduler(seeded, rabbit)
    task = asyncio.create_task(scheduler.run(stop))
    try:
        alert = await make_alert(seeded, await make_rule(seeded))

        async def reached_master():
            row = await alert_row(seeded, alert["id"])
            return row["escalation_level"] == 3

        await wait_for(reached_master, wait_s=15, what="escalation to level 3")
    finally:
        stop.set()
        await task
        await seeded.execute(
            "DELETE FROM escalation_policies WHERE scope_type = 'team' AND scope_id = $1", SETTLEMENTS
        )
    notified = {d["recipient_user_id"] for d in await deliveries(seeded, alert["id"], kind="hard")}
    assert notified == {user("piotr@demo"), user("root.settlements@demo"), user("master@demo")}


def test_digest_interval_from_environment(monkeypatch):
    monkeypatch.delenv("DIGEST_INTERVAL_SECONDS", raising=False)
    assert digest_interval() == 300
    monkeypatch.setenv("DIGEST_INTERVAL_SECONDS", "7")
    assert digest_interval() == 7
    monkeypatch.setenv("DIGEST_INTERVAL_SECONDS", "soon")
    assert digest_interval() == 300


async def test_tick_runs_every_job_and_survives_a_failing_one(seeded, fake_rabbit, monkeypatch):
    calls: list[str] = []

    async def failing(*args, **kwargs):
        calls.append("escalate")
        raise RuntimeError("boom")

    async def counting(*args, **kwargs):
        calls.append("other")
        return 0

    monkeypatch.setattr(jobs, "escalate_due", failing)
    for name in ("renotify_due", "expire_approvals", "materialise_shifts", "send_digests"):
        monkeypatch.setattr(jobs, name, counting)
    scheduler = Scheduler(seeded, fake_rabbit)
    await scheduler.tick()
    assert calls.count("escalate") == 1
    assert calls.count("other") == 4  # renotify, approvals, shifts, digests: the failure did not stop them
    calls.clear()
    await scheduler.tick()  # shifts and digests are not due again yet
    assert calls.count("other") == 2


async def test_failing_renotify_is_deferred_not_looped(seeded, fake_rabbit, monkeypatch):
    alert = await make_alert(seeded, await make_rule(seeded))
    await make_due(seeded, alert["id"])
    await seeded.execute(
        "UPDATE alerts SET ack_deadline = now() + interval '1 hour' WHERE id = $1", alert["id"]
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("planning broke")

    monkeypatch.setattr(jobs, "plan_deliveries", boom)
    assert await jobs.renotify_due(seeded, fake_rabbit) == 0
    assert (await alert_row(seeded, alert["id"]))["next_renotify_at"] > datetime.now(UTC)
    assert await timeline_texts(seeded, alert["id"], "notified") == []


def test_human_duration():
    assert jobs.human_duration(300) == "5 min"
    assert jobs.human_duration(90) == "90 s"
    assert jobs.human_duration(None) == "no ack"


async def test_expire_approvals_delegates_to_the_approvals_module(seeded, monkeypatch):
    calls: list[object] = []

    async def expire_due(conn):
        calls.append(conn)
        return [uuid7(), uuid7()]

    package = types.ModuleType("app.runbooks")
    package.__path__ = []
    module = types.ModuleType("app.runbooks.approvals")
    module.expire_due = expire_due
    monkeypatch.setitem(sys.modules, "app.runbooks", package)
    monkeypatch.setitem(sys.modules, "app.runbooks.approvals", module)
    assert await jobs.expire_approvals(seeded) == 2
    assert len(calls) == 1
