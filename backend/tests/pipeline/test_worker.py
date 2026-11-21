"""Worker hot path against real Postgres / Redis / RabbitMQ (rules + delivery planner are fakes)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import orjson
import pytest

from app.core.metrics import PROCESSING_LATENCY
from app.core.rabbit import Rabbit
from app.core.redis import rkey
from app.pipeline.envelope import build_envelope
from app.pipeline.worker import Worker
from tests.pipeline.conftest import alerts_for, eventually, fake_render

SAMPLES = Path(__file__).resolve().parents[2] / "seed" / "fixtures" / "samples"


def sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text())


WORKFLOW_RULE = {
    "priority": "error",
    "match": {
        "op": "and",
        "conditions": [
            {"field": "type", "operator": "eq", "value": "WORKFLOW_FAILED"},
            {"field": "environment", "operator": "eq", "value": "prod"},
        ],
    },
    "presentation": {
        "titleTemplate": "{{workflowName}} failed at {{step}}",
        "bodyTemplate": "Error {{error.code}}: {{error.message}}",
        "fields": ["step", "error.message", "nope"],
        "color": "red",
    },
    "runbookId": None,
}


async def alert_count(pool, rule) -> int:
    return len(await alerts_for(pool, rule))


async def test_first_event_creates_alert_with_everything(
    worker_env, make_source, make_rule, planner, outcomes
):
    rule = await make_rule(WORKFLOW_RULE)
    source = await make_source()
    planner.outbox_routing_key = "ui"
    pubsub = worker_env.redis.pubsub()
    channel = rkey("sse", "team", str(source["team_id"]))
    await pubsub.subscribe(channel)
    await pubsub.get_message(timeout=1)

    envelope = await worker_env.publish(source, sample("workflow_failed"))
    (alert,) = await eventually(lambda: alerts_for(worker_env.pool, rule))

    assert alert["title"] == "order-settlement failed at validate-counterparty"
    assert alert["body"] == "Error TIMEOUT: Counterparty service did not respond in 5000 ms"
    assert alert["severity"] == "error" and alert["color"] == "red" and alert["status"] == "open"
    assert alert["count"] == 1
    assert alert["team_id"] == source["team_id"] and alert["domain_id"] == source["domain_id"]
    assert alert["source_id"] == source["id"]
    assert alert["fields"] == [
        {"label": "step", "value": "validate-counterparty"},
        {"label": "error.message", "value": "Counterparty service did not respond in 5000 ms"},
    ]
    assert alert["raw_event"]["workflowId"] == "WF-20261023-00042"
    assert alert["knowledge_key"] and alert["flags"] == []
    assert alert["first_event_received_at"].isoformat().startswith(envelope["received_at"][:19])
    assert planner.reasons == ["created"]
    assert planner.calls[0]["alert"]["id"] == alert["id"]
    assert planner.calls[0]["rule"] is rule.rule

    timeline = await worker_env.pool.fetch("SELECT type, text FROM timeline WHERE alert_id = $1", alert["id"])
    assert [t["type"] for t in timeline] == ["created", "assigned"]  # the on-call owns it right away

    async def published_outbox():
        rows = await worker_env.pool.fetch(
            "SELECT topic, routing_key, published_at FROM outbox WHERE payload::text LIKE $1",
            f"%{alert['id']}%",
        )
        # rows are published right after the commit that made the alert visible: poll, don't race
        return rows if rows and all(r["published_at"] is not None for r in rows) else None

    outbox = await eventually(published_outbox)
    topics = {(o["topic"], o["routing_key"]) for o in outbox}
    assert ("ai", "ai.tasks") in topics and ("deliveries", "ui") in topics
    assert ("sse", f"sse:team:{source['team_id']}") in topics
    assert all(o["published_at"] is not None for o in outbox)

    message = None
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if message:
            break
    body = orjson.loads(message["data"])
    assert body["type"] == "alert.created"
    assert body["data"]["id"] == str(alert["id"]) and body["data"]["count"] == 1
    assert body["data"]["title"] == alert["title"]
    await pubsub.aclose()

    await eventually(lambda: _value(outcomes["alerted_new"] == 1))


async def _value(flag: bool) -> bool:
    return flag


async def test_500_identical_events_one_alert_count_500_one_delivery_plan(
    worker_env, make_source, make_rule, planner, outcomes, redis
):
    rule = await make_rule(WORKFLOW_RULE)
    source = await make_source()
    pubsub = redis.pubsub()
    await pubsub.subscribe(rkey("sse", "team", str(source["team_id"])))
    await pubsub.get_message(timeout=1)

    envelopes = [
        build_envelope(source_id=source["id"], raw=sample("workflow_failed"), priority=6) for _ in range(500)
    ]
    await worker_env.rabbit.publish_batch("events", "events.in", envelopes, priority=6)

    async def finished() -> bool:
        rows = await alerts_for(worker_env.pool, rule)
        return len(rows) == 1 and rows[0]["count"] == 500

    await eventually(finished, timeout=60)
    (alert,) = await alerts_for(worker_env.pool, rule)
    assert alert["count"] == 500
    assert planner.reasons == ["created"]
    assert (
        await worker_env.pool.fetchval("SELECT count(*) FROM timeline WHERE alert_id = $1", alert["id"]) == 2
    )  # created + the automatic assignment, nothing per duplicate
    await eventually(lambda: _value(outcomes["alerted_new"] == 1 and outcomes["deduplicated"] == 499))

    # SSE: count changes are throttled (<= 1 per 250 ms) but the final count always arrives
    await asyncio.sleep(0.6)
    updates = []
    while message := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2):
        updates.append(orjson.loads(message["data"]))
    kinds = [u["type"] for u in updates]
    assert kinds.count("alert.created") == 1
    assert 1 <= kinds.count("alert.updated") < 60
    assert updates[-1]["data"]["count"] == 500
    await pubsub.aclose()


async def test_same_event_id_twice_is_processed_once(
    worker_env, make_source, make_rule, planner, outcomes, redis
):
    rule = await make_rule(WORKFLOW_RULE)
    source = await make_source()
    envelope = build_envelope(source_id=source["id"], raw=sample("workflow_failed"), priority=6)
    for _ in range(2):
        await worker_env.rabbit.publish("events", "events.in", envelope, priority=6)
    await eventually(lambda: _value(outcomes["alerted_new"] == 1))
    await asyncio.sleep(0.5)
    (alert,) = await alerts_for(worker_env.pool, rule)
    assert alert["count"] == 1
    assert outcomes["deduplicated"] == 0
    assert await redis.get(rkey("evt", envelope["event_id"])) == "1"
    # a later redelivery of a finished event is skipped too
    assert await worker_env.worker.process(orjson.dumps(envelope), redelivered=True) == "duplicate"


async def test_redelivered_message_takes_over_a_crashed_claim(worker_env, make_source, make_rule, redis):
    rule = await make_rule(WORKFLOW_RULE)
    source = await make_source()
    envelope = build_envelope(source_id=source["id"], raw=sample("workflow_failed"), priority=6)
    await redis.set(rkey("evt", envelope["event_id"]), "p", ex=60)  # claimed by a worker that then died
    assert await worker_env.worker.process(orjson.dumps(envelope), redelivered=False) == "duplicate"
    assert await worker_env.worker.process(orjson.dumps(envelope), redelivered=True) == "alerted_new"
    assert await alert_count(worker_env.pool, rule) == 1
    assert await redis.get(rkey("evt", envelope["event_id"])) == "1"


async def test_unrouted_is_counted_and_recorded(worker_env, make_source, make_rule, outcomes, redis):
    await make_rule(WORKFLOW_RULE)
    source = await make_source()
    stats_key = rkey("stats", "source", str(source["id"]))
    envelope = await worker_env.publish(source, {"type": "SOMETHING_ELSE", "environment": "dev"})
    await eventually(lambda: _value(outcomes["unrouted"] == 1))
    await eventually(lambda: redis.hget(stats_key, "unrouted"))
    assert await redis.hget(stats_key, "unrouted") == "1"
    entries = await redis.xrange(rkey("recent", str(source["id"])))
    assert len(entries) == 1
    recent = orjson.loads(entries[0][1]["envelope"])
    assert recent["event_id"] == envelope["event_id"]
    assert recent["_meta"]["schema"] == "SOMETHING_ELSE"
    assert recent["raw"] == {"type": "SOMETHING_ELSE", "environment": "dev"}
    assert (
        await worker_env.pool.fetchval("SELECT count(*) FROM alerts WHERE source_id = $1", source["id"]) == 0
    )


async def test_unknown_source_and_garbage_are_invalid(worker_env, rabbit, outcomes):
    ghost = {"id": uuid4()}
    await worker_env.publish(ghost, {"type": "X"})
    await rabbit.publish("events", "events.in", b"{not json", priority=1)
    await rabbit.publish("events", "events.in", {"event_id": "e", "raw": []}, priority=1)
    await eventually(lambda: _value(outcomes["invalid"] == 3))


async def test_mapping_is_applied_and_drives_severity_and_title(worker_env, make_source, make_rule):
    rule = await make_rule(
        {
            "match": {"op": "and", "conditions": [{"field": "_service", "operator": "eq", "value": "gw"}]},
            "presentation": {"fields": ["_environment", "_error_code"]},
        }
    )
    mapping = {
        "detectedSchema": "x",
        "fields": {
            "severity": "labels.lvl",
            "service": "meta.app",
            "title": "annotations.what",
            "environment": "env",
            "error_code": "err",
        },
        "severityValueMap": {"SEV1": "critical"},
    }
    source = await make_source(mapping=mapping, mapping_status="accepted")
    await worker_env.publish(
        source,
        {
            "labels": {"lvl": "SEV1"},
            "meta": {"app": "gw"},
            "annotations": {"what": "gateway down"},
            "env": "prod",
            "err": "E42",
        },
    )
    (alert,) = await eventually(lambda: alerts_for(worker_env.pool, rule))
    assert alert["title"] == "gateway down"
    assert alert["severity"] == "critical"
    assert {f["label"]: f["value"] for f in alert["fields"]} == {"_environment": "prod", "_error_code": "E42"}


async def test_draft_mapping_is_not_applied(worker_env, make_source, make_rule):
    rule = await make_rule(
        {"match": {"op": "and", "conditions": [{"field": "_service", "operator": "eq", "value": "gw"}]}}
    )
    mapping = {"fields": {"service": "meta.app"}}
    source = await make_source(mapping=mapping, mapping_status="draft", default_severity="info")
    await worker_env.publish(source, {"meta": {"app": "gw"}, "message": "m"})
    await asyncio.sleep(0.5)
    assert await alert_count(worker_env.pool, rule) == 0


async def test_dedup_fields_and_severity_increase_notify(
    worker_env, make_source, make_rule, planner, outcomes
):
    rule = await make_rule(
        {
            "match": {"op": "and", "conditions": [{"field": "kind", "operator": "eq", "value": "limit"}]},
            "presentation": {"titleTemplate": "Limit {{desk}}"},
            "dedup": {"fields": ["tradeId"]},
        }
    )
    source = await make_source()
    await worker_env.publish(source, {"kind": "limit", "desk": "rates", "tradeId": "T-1", "severity": "warn"})
    await worker_env.publish(source, {"kind": "limit", "desk": "fx", "tradeId": "T-1", "severity": "warn"})
    await worker_env.publish(source, {"kind": "limit", "desk": "rates", "tradeId": "T-2", "severity": "warn"})
    await eventually(lambda: _value(outcomes["alerted_new"] == 2 and outcomes["deduplicated"] == 1))
    alerts = await alerts_for(worker_env.pool, rule)
    assert sorted(a["count"] for a in alerts) == [1, 2]
    assert planner.reasons == ["created", "created"]

    await worker_env.publish(
        source, {"kind": "limit", "desk": "rates", "tradeId": "T-1", "severity": "FATAL"}
    )
    await eventually(lambda: _value("severity_up" in planner.reasons))
    row = await worker_env.pool.fetchrow(
        "SELECT severity, count FROM alerts WHERE rule_id = $1 AND count = 3", rule.rule.id
    )
    assert row["severity"] == "critical"
    # a later lower severity never lowers it and never notifies again
    await worker_env.publish(source, {"kind": "limit", "desk": "rates", "tradeId": "T-1", "severity": "info"})
    await eventually(
        lambda: worker_env.pool.fetchrow(
            "SELECT 1 FROM alerts WHERE rule_id = $1 AND count = 4", rule.rule.id
        )
    )
    assert (
        await worker_env.pool.fetchval(
            "SELECT severity FROM alerts WHERE rule_id = $1 AND count = 4", rule.rule.id
        )
    ) == "critical"
    assert planner.reasons.count("severity_up") == 1


async def test_grouping_by_field_appends_items_and_caps_at_100(worker_env, make_source, make_rule, outcomes):
    rule = await make_rule(
        {
            "match": {
                "op": "and",
                "conditions": [{"field": "logger", "operator": "glob", "value": "pricing-engine-*"}],
            },
            "presentation": {"titleTemplate": "Pricing issues for {{instrument}}"},
            "grouping": {"byField": "instrument", "windowSeconds": 300},
        },
        team="team-pricing",
    )
    source = await make_source("team-pricing")
    for _ in range(105):
        await worker_env.publish(source, {**sample("pricing_log"), "instrument": "EURUSD"})
    await worker_env.publish(source, {**sample("pricing_log"), "instrument": "GBPUSD"})

    async def done() -> bool:
        rows = await alerts_for(worker_env.pool, rule)
        return sorted(r["count"] for r in rows) == [1, 105]

    await eventually(done)
    by_title = {a["title"]: a for a in await alerts_for(worker_env.pool, rule)}
    eur, gbp = by_title["Pricing issues for EURUSD"], by_title["Pricing issues for GBPUSD"]
    assert len(eur["grouped_items"]) == 100 and len(gbp["grouped_items"]) == 1
    assert gbp["grouped_items"][0]["instrument"] == "GBPUSD"
    assert "grouped" in eur["flags"]
    assert outcomes["alerted_new"] == 2 and outcomes["grouped"] == 104


async def test_event_matching_two_rules_creates_two_alerts_one_outcome(
    worker_env, make_source, make_rule, outcomes
):
    cond = {"op": "and", "conditions": [{"field": "type", "operator": "eq", "value": "WORKFLOW_FAILED"}]}
    rule_a = await make_rule({"match": cond}, team="team-settlements")
    rule_b = await make_rule({"match": cond}, team="team-payments")
    source = await make_source("team-settlements")
    await worker_env.publish(source, sample("workflow_failed"))
    await eventually(lambda: _value(outcomes["alerted_new"] == 1))
    assert await alert_count(worker_env.pool, rule_a) == 1 and await alert_count(worker_env.pool, rule_b) == 1
    payments_alert = (await alerts_for(worker_env.pool, rule_b))[0]
    # the rule's team (payments) is not the source's: the alert gets one of the team's own domains
    payments_domains = await worker_env.pool.fetch(
        "SELECT domain_id FROM team_domains WHERE team_id = $1", rule_b.rule.team_id
    )
    assert payments_alert["domain_id"] in {r["domain_id"] for r in payments_domains}
    assert outcomes["deduplicated"] == 0


async def test_injection_in_event_flags_the_alert(worker_env, make_source, make_rule):
    rule = await make_rule(
        {
            "match": {
                "op": "and",
                "conditions": [{"field": "logger", "operator": "glob", "value": "pricing-*"}],
            }
        }
    )
    source = await make_source()
    await worker_env.publish(source, sample("pricing_log_injection"))
    (alert,) = await eventually(lambda: alerts_for(worker_env.pool, rule))
    assert "suspicious_content" in alert["flags"]
    timeline = await worker_env.pool.fetch("SELECT text FROM timeline WHERE alert_id = $1", alert["id"])
    assert any("Suspicious content" in t["text"] for t in timeline)

    clean = await make_rule(
        {"match": {"op": "and", "conditions": [{"field": "level", "operator": "eq", "value": "ERROR"}]}}
    )
    await worker_env.publish(source, sample("pricing_log"))
    (clean_alert,) = await eventually(lambda: alerts_for(worker_env.pool, clean))
    assert clean_alert["flags"] == []


async def test_typed_event_is_prenormalized(worker_env, make_source, make_rule):
    rule = await make_rule(
        {
            "match": {
                "op": "and",
                "conditions": [{"field": "_type", "operator": "glob", "value": "security.*"}],
            }
        }
    )
    source = await make_source("team-security")
    raw = sample("raise_secret_leaked")
    meta = {
        "source": source["name"],
        "schema": raw["type"],
        "type": raw["type"],
        "severity": "critical",
        "title": raw["title"],
        "service": "payments-api",
    }
    for _ in range(2):
        await worker_env.publish(source, raw, typed=True, meta=meta)
    (alert,) = await eventually(lambda: _both(worker_env.pool, rule))
    assert alert["title"] == raw["title"] and alert["body"] == raw["description"]
    assert alert["severity"] == "critical"
    assert set(alert["flags"]) == {"from_agent"}
    assert alert["count"] == 2  # same dedupKey


async def _both(pool, rule):
    rows = await alerts_for(pool, rule)
    return rows if rows and rows[0]["count"] == 2 else None


async def test_processing_latency_is_observed(worker_env, make_source, make_rule):
    rule = await make_rule(WORKFLOW_RULE)
    source = await make_source()
    before = PROCESSING_LATENCY._sum.get()
    await worker_env.publish(source, sample("workflow_failed"))
    await eventually(lambda: alerts_for(worker_env.pool, rule))
    await eventually(lambda: _value(PROCESSING_LATENCY._sum.get() > before))


class FakeMessage:
    def __init__(self, body: bytes, redelivered: bool = False) -> None:
        self.body, self.redelivered = body, redelivered
        self.calls: list[str] = []

    async def ack(self) -> None:
        self.calls.append("ack")

    async def nack(self, requeue: bool = False) -> None:
        self.calls.append(f"nack(requeue={requeue})")


@pytest.fixture
def direct_worker(seeded, redis, index, planner):
    return Worker(seeded, Rabbit(), redis, index=index, render=fake_render, planner=planner)


async def test_unexpected_error_requeues_once_then_counts_invalid_and_acks(
    direct_worker, make_source, make_rule, planner, outcomes, redis
):
    rule = await make_rule(WORKFLOW_RULE)
    source = await make_source()
    planner.fail_with = ValueError("poison")
    envelope = build_envelope(source_id=source["id"], raw=sample("workflow_failed"), priority=6)
    body = orjson.dumps(envelope)

    first = FakeMessage(body)
    await direct_worker.handle(first)
    assert first.calls == ["nack(requeue=True)"]
    assert await redis.get(rkey("evt", envelope["event_id"])) is None  # claim released for the retry
    second = FakeMessage(body, redelivered=True)
    await direct_worker.handle(second)
    assert second.calls == ["ack"]
    assert outcomes["invalid"] == 1
    assert await alert_count(direct_worker.pool, rule) == 0  # the failed transaction left nothing behind

    planner.fail_with = None
    third = FakeMessage(body)
    await direct_worker.handle(third)
    assert third.calls == ["ack"] and await alert_count(direct_worker.pool, rule) == 1


async def test_dependency_outage_always_requeues(direct_worker, make_source, make_rule, planner, monkeypatch):
    await make_rule(WORKFLOW_RULE)
    source = await make_source()
    planner.fail_with = ConnectionResetError("db went away")
    monkeypatch.setattr("app.pipeline.worker.TRANSIENT_BACKOFF", 0)
    envelope = build_envelope(source_id=source["id"], raw=sample("workflow_failed"), priority=6)
    for redelivered in (False, True, True):
        msg = FakeMessage(orjson.dumps(envelope), redelivered=redelivered)
        await direct_worker.handle(msg)
        assert msg.calls == ["nack(requeue=True)"]


async def test_broken_rule_does_not_stop_other_rules(direct_worker, make_source, make_rule):
    class Broken:
        rule = WORKFLOW_RULE

        def matches(self, ev):
            raise RuntimeError("bad regex")

    good = await make_rule(WORKFLOW_RULE)
    direct_worker.index.rules.insert(0, Broken())
    source = await make_source()
    envelope = build_envelope(source_id=source["id"], raw=sample("workflow_failed"), priority=6)
    assert await direct_worker.process(orjson.dumps(envelope)) == "alerted_new"
    assert await alert_count(direct_worker.pool, good) == 1
