"""A new alert gets its incident manager in the same transaction; duplicates never reassign."""

from __future__ import annotations

from app.core.ids import stable_id
from tests.pipeline.conftest import alerts_for, eventually
from tests.pipeline.test_worker import WORKFLOW_RULE, sample

PIOTR = stable_id("user", "piotr@demo")
ROOT = stable_id("user", "root.settlements@demo")


async def assigned_entries(pool, alert_id):
    return await pool.fetch(
        "SELECT actor, text, data FROM timeline WHERE alert_id = $1 AND type = 'assigned' ORDER BY at, id",
        alert_id,
    )


async def test_new_alert_is_assigned_to_the_on_call_and_dedup_keeps_the_owner(
    worker_env, make_source, make_rule, planner
):
    rule = await make_rule(WORKFLOW_RULE)
    source = await make_source()
    await worker_env.publish(source, sample("workflow_failed"))
    (alert,) = await eventually(lambda: alerts_for(worker_env.pool, rule))

    assert alert["assignee_id"] == PIOTR
    assert alert["assign_reason"] == "on call for Team Settlements"
    assert alert["assigned_by"]["name"] == "auto-assign"
    assert planner.calls[0]["alert"]["assignee_id"] == PIOTR
    (entry,) = await assigned_entries(worker_env.pool, alert["id"])
    assert entry["text"] == "Assigned to Piotr Wisniewski automatically: on call for Team Settlements"
    assert entry["data"]["auto"] is True

    # a person hands it over; the next duplicates neither reassign nor add timeline entries
    await worker_env.pool.execute(
        "UPDATE alerts SET assignee_id = $2, assigned_by = $3, assign_reason = NULL WHERE id = $1",
        alert["id"],
        ROOT,
        {"type": "user", "id": str(ROOT), "name": "Stefan Settlements"},
    )
    for _ in range(3):
        await worker_env.publish(source, sample("workflow_failed"))

    async def counted():
        (row,) = await alerts_for(worker_env.pool, rule)
        return row if row["count"] == 4 else None

    row = await eventually(counted)
    assert row["assignee_id"] == ROOT
    assert len(await assigned_entries(worker_env.pool, alert["id"])) == 1
    assert (
        await worker_env.pool.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'alert.auto_assign' AND target_id = $1",
            str(alert["id"]),
        )
        == 1
    )
