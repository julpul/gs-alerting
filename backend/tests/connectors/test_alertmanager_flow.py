"""Alertmanager preset end to end: one event per alert at ingest; resolved -> auto-resolve."""

from __future__ import annotations

import asyncio

import orjson

from app.core.metrics import EVENTS_OUTCOME, INGESTED
from app.core.redis import rkey
from tests.connectors.conftest import eventually
from tests.connectors.test_presets import am_alert, am_group
from tests.pipeline.conftest import alerts_for

RULE = {
    "priority": "error",
    "match": {
        "op": "and",
        "conditions": [{"field": "labels.severity", "operator": "eq", "value": "critical"}],
    },
    "presentation": {"titleTemplate": "{{alertname}}: {{annotations.summary}}"},
    "dedup": {"fields": ["alertname", "labels.service"]},
}


def outcome(label: str) -> float:
    return EVENTS_OUTCOME.labels(label)._value.get()


async def ingest(client, source: dict, payload) -> dict:
    suffix = "/batch" if isinstance(payload, list) else ""
    resp = await client.post(f"/v1/ingest/{source['key']}{suffix}", json=payload)
    assert resp.status_code == 202, resp.text
    return resp.json()


# ---- split at ingest ------------------------------------------------------------------------------------


async def test_grouped_alertmanager_payload_with_three_alerts_is_three_events(
    client, make_preset_source, tap
):
    source = await make_preset_source("alertmanager")
    before = INGESTED.labels(source=source["name"])._value.get()
    group = am_group(am_alert("A", "x"), am_alert("B", "y"), am_alert("C", "z"))
    body = await ingest(client, source, group)

    envelopes = await tap.wait_for(3, source)
    assert sorted(e["raw"]["alertname"] for e in envelopes) == ["A", "B", "C"]
    assert len({e["event_id"] for e in envelopes}) == 3
    assert body["eventId"] in {e["event_id"] for e in envelopes}
    assert INGESTED.labels(source=source["name"])._value.get() - before == 3


async def test_single_alert_brief_shape_passes_through_unchanged(client, make_preset_source, tap):
    source = await make_preset_source("alertmanager")
    brief = {
        "alertname": "HighErrorRate",
        "labels": {"service": "payments-api", "severity": "critical"},
        "annotations": {"summary": "Error rate 12%"},
        "status": "firing",
    }
    await ingest(client, source, brief)
    (envelope,) = await tap.wait_for(1, source)
    assert envelope["raw"] == brief


async def test_batch_endpoint_splits_each_payload(client, make_preset_source, tap):
    source = await make_preset_source("alertmanager")
    payloads = [
        am_group(am_alert("A", "x"), am_alert("B", "y"), am_alert("C", "z")),
        am_group(am_alert("D", "w")),
    ]
    body = await ingest(client, source, payloads)
    assert body["accepted"] == 4 and len(body["eventIds"]) == 4
    assert len(await tap.wait_for(4, source)) == 4


async def test_other_presets_and_plain_webhooks_are_not_split(client, make_preset_source, tap):
    plain = await make_preset_source()
    sentry = await make_preset_source("sentry")
    payload = {"alerts": [{"a": 1}, {"a": 2}]}
    await ingest(client, plain, payload)
    await ingest(client, sentry, payload)
    assert [e["raw"] for e in await tap.wait_for(1, plain)] == [payload]
    assert [e["raw"] for e in await tap.wait_for(1, sentry)] == [payload]


# ---- auto-resolve -------------------------------------------------------------------------------------


async def test_firing_then_resolved_resolves_the_alert_and_creates_no_new_one(
    worker_env, client, make_preset_source, make_rule, planner
):
    rule = await make_rule(RULE, team="team-payments")
    source = await make_preset_source("alertmanager")
    pubsub = worker_env.redis.pubsub()
    await pubsub.subscribe(rkey("sse", "team", str(source["team_id"])))
    await pubsub.get_message(timeout=1)
    resolved_before = outcome("auto_resolved")

    await ingest(client, source, am_group(am_alert("HighErrorRate", "payments-api")))
    (alert,) = await eventually(lambda: alerts_for(worker_env.pool, rule))
    assert alert["status"] == "open"

    await ingest(
        client,
        source,
        am_group(am_alert("HighErrorRate", "payments-api", status="resolved"), status="resolved"),
    )
    await eventually(lambda: _flag(outcome("auto_resolved") - resolved_before == 1))

    (alert,) = await alerts_for(worker_env.pool, rule)  # still exactly one alert
    assert alert["status"] == "resolved" and alert["resolved_at"] is not None
    assert alert["next_renotify_at"] is None and alert["ack_deadline"] is None
    assert alert["count"] == 1  # the resolved notification is not a recurrence
    assert planner.reasons == ["created"]

    timeline = await worker_env.pool.fetch(
        "SELECT type, text, actor FROM timeline WHERE alert_id = $1 ORDER BY at", alert["id"]
    )
    assert [t["type"] for t in timeline] == ["created", "resolved"]
    assert timeline[-1]["text"] == "Resolved by source (Alertmanager status=resolved)"
    assert timeline[-1]["actor"]["type"] == "system"
    audit = await worker_env.pool.fetchrow(
        "SELECT actor FROM audit_log WHERE action = 'alert.resolve' AND target_id = $1", str(alert["id"])
    )
    assert audit["actor"]["type"] == "system"
    assert (
        await worker_env.pool.fetchval("SELECT count(*) FROM resolutions WHERE alert_id = $1", alert["id"])
        == 0
    )

    sse = await _sse_types(pubsub)
    assert "alert.created" in sse and "alert.resolved" in sse
    await pubsub.aclose()


async def test_resolved_notification_resolves_only_the_alerts_it_names(
    worker_env, client, make_preset_source, make_rule
):
    rule = await make_rule(RULE, team="team-payments")
    source = await make_preset_source("alertmanager")
    resolved_before = outcome("auto_resolved")
    fired = [am_alert("A", "x"), am_alert("B", "y"), am_alert("C", "z")]
    await ingest(client, source, am_group(*fired))
    await eventually(lambda: _flag(_count(worker_env.pool, rule, 3)))

    done = [{**fired[0], "status": "resolved"}, {**fired[2], "status": "resolved"}]
    await ingest(client, source, am_group(*done, status="resolved"))
    await eventually(lambda: _flag(outcome("auto_resolved") - resolved_before == 2))

    alerts = await alerts_for(worker_env.pool, rule)
    status = {a["title"].split(":")[0]: a["status"] for a in alerts}
    assert status == {"A": "resolved", "B": "open", "C": "resolved"}


async def test_acknowledged_alert_is_resolved_too(worker_env, client, make_preset_source, make_rule):
    rule = await make_rule(RULE, team="team-payments")
    source = await make_preset_source("alertmanager")
    resolved_before = outcome("auto_resolved")
    await ingest(client, source, am_group(am_alert("A", "x")))
    (alert,) = await eventually(lambda: alerts_for(worker_env.pool, rule))
    await worker_env.pool.execute("UPDATE alerts SET status = 'acknowledged' WHERE id = $1", alert["id"])

    await ingest(client, source, am_group(am_alert("A", "x", status="resolved"), status="resolved"))
    await eventually(lambda: _flag(outcome("auto_resolved") - resolved_before == 1))
    assert (await alerts_for(worker_env.pool, rule))[0]["status"] == "resolved"


async def test_resolved_without_an_open_alert_creates_nothing(
    worker_env, client, make_preset_source, make_rule
):
    rule = await make_rule(RULE, team="team-payments")
    source = await make_preset_source("alertmanager")
    unrouted_before = outcome("unrouted")
    await ingest(client, source, am_group(am_alert("A", "x", status="resolved"), status="resolved"))
    await eventually(lambda: _flag(outcome("unrouted") - unrouted_before == 1))
    assert await alerts_for(worker_env.pool, rule) == []


async def test_a_new_firing_after_resolve_opens_a_new_alert(
    worker_env, client, make_preset_source, make_rule
):
    rule = await make_rule(RULE, team="team-payments")
    source = await make_preset_source("alertmanager")
    resolved_before = outcome("auto_resolved")
    await ingest(client, source, am_group(am_alert("A", "x")))
    await eventually(lambda: alerts_for(worker_env.pool, rule))
    await ingest(client, source, am_group(am_alert("A", "x", status="resolved"), status="resolved"))
    await eventually(lambda: _flag(outcome("auto_resolved") - resolved_before == 1))
    await ingest(client, source, am_group(am_alert("A", "x")))
    await eventually(lambda: _flag(_statuses(worker_env.pool, rule, ["resolved", "open"])))


async def test_resolved_status_on_a_plain_webhook_source_is_just_an_event(
    worker_env, client, make_preset_source, make_rule
):
    rule = await make_rule(RULE, team="team-payments")
    source = await make_preset_source()  # no preset: no auto-resolve semantics
    before = outcome("auto_resolved")
    await ingest(client, source, {"alertname": "A", "status": "resolved", "labels": {"severity": "critical"}})
    (alert,) = await eventually(lambda: alerts_for(worker_env.pool, rule))
    assert alert["status"] == "open" and outcome("auto_resolved") == before


async def _flag(value) -> bool:
    return await value if asyncio.iscoroutine(value) else bool(value)


async def _count(pool, rule, expected: int) -> bool:
    return len(await alerts_for(pool, rule)) == expected


async def _statuses(pool, rule, expected: list[str]) -> bool:
    return [a["status"] for a in await alerts_for(pool, rule)] == expected


async def _sse_types(pubsub) -> list[str]:
    types: list[str] = []
    for _ in range(40):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.25)
        if message:
            types.append(orjson.loads(message["data"])["type"])
            if types[-1] == "alert.resolved":
                break
    return types
