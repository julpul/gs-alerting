"""Delivery worker end to end: real RabbitMQ, Postgres, Redis, Mailpit and the real mockgw app."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import orjson
import pytest
from prometheus_client import REGISTRY

from app.core.outbox import publish_outbox_rows
from app.core.sse import team_channel
from app.delivery.channels import registry
from app.delivery.channels.base import ChannelError, Message, Recipient, SendResult
from app.delivery.channels.webhook import WebhookAdapter, sign
from app.delivery.render import SMS_LIMIT
from tests.delivery.helpers import mailpit_search, make_alert, plan, rule_def, team, user, wait_for
from tests.delivery.test_routing import unique_minute

PIOTR = {"kind": "oncall", "id": "team-settlements"}
SMS_RULE = rule_def(hard=[PIOTR], channels={"critical": ["sms"]})


@pytest.fixture(autouse=True)
async def settle_outbox(seeded):
    yield
    await seeded.execute("UPDATE outbox SET published_at = now() WHERE published_at IS NULL")


async def status_of(pool, delivery_id) -> str:
    return await pool.fetchval("SELECT status FROM deliveries WHERE id = $1", delivery_id)


async def plan_one(pool, channel: str, rule=SMS_RULE, **alert_kw) -> tuple[dict, dict]:
    alert = await make_alert(pool, **alert_kw)
    rows = await plan(pool, alert, rule, reason="created", now=unique_minute())
    return alert, next(r for r in rows if r["channel"] == channel)


async def publish(pool, rabbit, rows) -> None:
    await publish_outbox_rows(pool, rabbit, [r["outbox_id"] for r in rows if r.get("outbox_id")])


async def timeline_texts(pool, alert_id) -> list[str]:
    return [
        r["text"]
        for r in await pool.fetch("SELECT text FROM timeline WHERE alert_id = $1 ORDER BY at", alert_id)
    ]


async def queue_depth(rabbit, base: str) -> int:
    channel = await rabbit._connection.channel()
    try:
        queue = await channel.get_queue(rabbit.name(base), ensure=True)
        return queue.declaration_result.message_count
    finally:
        await channel.close()


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# --- success paths ------------------------------------------------------------------------------------


async def test_email_actually_arrives_in_mailpit_with_links_and_runbook_placeholder(seeded, rabbit, worker):
    token = uuid.uuid4().hex[:10]
    rule = rule_def(hard=[{"kind": "user", "id": "anna@demo"}], channels={"error": ["email"]})
    alert = await make_alert(seeded, "team-payments", severity="error", title=f"Rates trade rejected {token}")
    rows = await plan(seeded, alert, rule, reason="created", now=unique_minute())
    await publish(seeded, rabbit, rows)
    email = next(r for r in rows if r["channel"] == "email")
    await wait_for(lambda: _is(seeded, email["id"], "delivered"), what="email delivered")

    found = await mailpit_search(f"subject:{token}")
    assert found[0]["To"][0]["Address"] == "anna@demo"
    assert found[0]["Subject"].startswith("[ERR] A-") and token in found[0]["Subject"]
    async with httpx.AsyncClient() as client:
        detail = (await client.get(f"http://localhost:8025/api/v1/message/{found[0]['ID']}")).json()
    assert "action=ack" in detail["HTML"] and "action=resolve" in detail["HTML"]
    assert "Runbook" in detail["HTML"] and "Acknowledge" in detail["Text"]
    assert await timeline_texts(seeded, alert["id"]) == ["Email delivered to Anna Kowalska"]


async def _is(pool, delivery_id, status) -> bool:
    return await status_of(pool, delivery_id) == status


async def test_ui_delivery_is_delivered_with_sse_and_metrics(seeded, rabbit, worker, redis):
    alert, ui = await plan_one(seeded, "ui")
    pubsub = redis.pubsub()
    await pubsub.subscribe(team_channel(team("team-settlements")))
    await pubsub.get_message(timeout=1)
    before = sample("alerting_deliveries_total", channel="ui", status="delivered")
    count_before = sample("alerting_delivery_latency_seconds_count", channel="ui")

    await publish(seeded, rabbit, [ui])
    await wait_for(lambda: _is(seeded, ui["id"], "delivered"), what="ui delivered")

    event = None
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if message and orjson.loads(message["data"])["data"]["id"] == str(ui["id"]):
            event = orjson.loads(message["data"])
            break
    await pubsub.aclose()
    assert event["type"] == "delivery.status"
    assert event["data"] | {"updatedAt": None} == {
        "id": str(ui["id"]),
        "alertId": str(alert["id"]),
        "recipient": None,
        "channel": "ui",
        "kind": "hard",
        "status": "delivered",
        "attempts": 1,
        "updatedAt": None,
    }
    assert sample("alerting_deliveries_total", channel="ui", status="delivered") == before + 1
    assert sample("alerting_delivery_latency_seconds_count", channel="ui") == count_before + 1
    row = await seeded.fetchrow("SELECT delivered_at, sent_at FROM deliveries WHERE id = $1", ui["id"])
    assert row["delivered_at"] is not None and row["sent_at"] is not None
    assert await timeline_texts(seeded, alert["id"]) == []  # the feed does not spam the timeline


async def test_sms_reaches_the_gateway_within_160_chars(seeded, rabbit, worker, gw):
    alert, sms = await plan_one(seeded, "sms", title="Settlement failed validate-counterparty " + "x" * 200)
    await publish(seeded, rabbit, [sms])
    await wait_for(lambda: _is(seeded, sms["id"], "sent"), what="sms sent")
    messages = (await gw.get("/admin/state")).json()["sms"]
    assert len(messages) == 1 and messages[0]["to"] == "+48500100201"
    assert messages[0]["idempotencyKey"] == sms["idempotency_key"]
    assert messages[0]["text"].startswith("[CRIT] A-") and len(messages[0]["text"]) <= SMS_LIMIT
    assert "Reply ACK" in messages[0]["text"]
    assert await timeline_texts(seeded, alert["id"]) == ["SMS sent to Piotr Wisniewski"]


async def test_redelivering_the_same_message_sends_one_sms(seeded, rabbit, worker, gw):
    _, sms = await plan_one(seeded, "sms")
    assert await worker.process(sms["id"]) == "sent"
    assert await worker.process(sms["id"]) == "skipped"  # already sent: skipped before any call
    await publish(seeded, rabbit, [sms])  # the very same message again, through the real consumer
    await asyncio.sleep(0.5)
    assert len((await gw.get("/admin/state")).json()["sms"]) == 1
    # crash between provider call and DB update: status is pending again, the provider still dedupes
    await seeded.execute("UPDATE deliveries SET status = 'pending' WHERE id = $1", sms["id"])
    assert await worker.process(sms["id"]) == "sent"
    state = (await gw.get("/admin/state")).json()
    assert len(state["sms"]) == 1


async def test_phone_call_reaches_the_gateway(seeded, rabbit, worker, gw):
    rule = rule_def(hard=[PIOTR], channels={"critical": ["phone"]})
    _, call = await plan_one(seeded, "phone", rule=rule)
    assert await worker.process(call["id"]) == "sent"
    calls = (await gw.get("/admin/state")).json()["calls"]
    assert len(calls) == 1 and "Press 1 to acknowledge" in calls[0]["text"]


async def test_suppressed_digest_and_missing_deliveries_are_not_sent(seeded, worker, gw):
    _, sms = await plan_one(seeded, "sms")
    await seeded.execute("UPDATE deliveries SET status = 'suppressed_digest' WHERE id = $1", sms["id"])
    assert await worker.process(sms["id"]) == "skipped"
    assert await worker.process(uuid.uuid4()) == "missing"
    assert (await gw.get("/admin/state")).json()["sms"] == []


# --- failure paths: retry, DLQ, fallback --------------------------------------------------------------


async def test_failing_sms_retries_then_dead_letters_and_falls_back_to_email(seeded, rabbit, worker, gw):
    token = uuid.uuid4().hex[:10]
    await gw.post("/admin/fail", json={"channel": "sms", "seconds": 120})
    alert, sms = await plan_one(seeded, "sms", title=f"Workflow stuck {token}")
    dlq_before = await queue_depth(rabbit, "deliveries.dlq")
    retry5_before = await queue_depth(rabbit, "deliveries.retry.5s")
    retry30_before = await queue_depth(rabbit, "deliveries.retry.30s")

    assert await worker.process(sms["id"]) == "retrying"
    assert await status_of(seeded, sms["id"]) == "retrying"
    assert await queue_depth(rabbit, "deliveries.retry.5s") == retry5_before + 1
    assert await worker.process(sms["id"]) == "retrying"
    assert await queue_depth(rabbit, "deliveries.retry.30s") == retry30_before + 1
    assert await worker.process(sms["id"]) == "dead_lettered"

    row = await seeded.fetchrow(
        "SELECT status, attempts, last_error FROM deliveries WHERE id = $1", sms["id"]
    )
    assert (row["status"], row["attempts"]) == ("dead_lettered", 3) and "503" in row["last_error"]
    assert await queue_depth(rabbit, "deliveries.dlq") == dlq_before + 1
    assert (await gw.get("/admin/state")).json()["sms"] == []
    assert "SMS gateway down, fell back to email" in await timeline_texts(seeded, alert["id"])

    fallback = await seeded.fetchrow(
        "SELECT * FROM deliveries WHERE alert_id = $1 AND channel = 'email'", alert["id"]
    )
    assert fallback["recipient_user_id"] == user("piotr@demo") and fallback["kind"] == "hard"
    assert fallback["idempotency_key"] == f"{alert['id']}:1:{user('piotr@demo')}:email"
    assert fallback["priority"] == 9 and fallback["event_received_at"] == alert["first_event_received_at"]
    # the fallback travelled through outbox -> RabbitMQ -> worker and landed in Mailpit
    await wait_for(lambda: _is(seeded, fallback["id"], "delivered"), what="fallback email delivered")
    found = await mailpit_search(f"subject:{token}")
    assert found[0]["To"][0]["Address"] == "piotr@demo"


async def test_retry_message_comes_back_from_the_ttl_queue_and_succeeds(seeded, rabbit, worker, gw):
    await gw.post("/admin/fail", json={"channel": "sms", "count": 1})
    _, sms = await plan_one(seeded, "sms")
    assert await worker.process(sms["id"]) == "retrying"
    await wait_for(lambda: _is(seeded, sms["id"], "sent"), wait_s=15, what="retry after the 5 s TTL")
    row = await seeded.fetchrow("SELECT attempts, last_error FROM deliveries WHERE id = $1", sms["id"])
    assert row["attempts"] == 2 and row["last_error"] is None
    assert len((await gw.get("/admin/state")).json()["sms"]) == 1


async def test_failing_phone_falls_back_to_sms(seeded, rabbit, worker, gw):
    await gw.post("/admin/fail", json={"channel": "phone", "seconds": 120})
    rule = rule_def(hard=[PIOTR], channels={"critical": ["phone"]})
    alert, call = await plan_one(seeded, "phone", rule=rule)
    outcomes = [await worker.process(call["id"]) for _ in range(3)]
    assert outcomes == ["retrying", "retrying", "dead_lettered"]
    fallback = await seeded.fetchrow(
        "SELECT * FROM deliveries WHERE alert_id = $1 AND channel = 'sms'", alert["id"]
    )
    assert fallback["recipient_address"] == "+48500100201"
    assert "Phone gateway down, fell back to sms" in await timeline_texts(seeded, alert["id"])
    await wait_for(lambda: _is(seeded, fallback["id"], "sent"), what="sms fallback sent")


async def test_dead_letter_without_anything_to_fall_back_to(seeded, rabbit, worker, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("SMTP_PORT", "1")
    get_settings.cache_clear()
    try:
        rule = rule_def(hard=[PIOTR], channels={"critical": ["email"]})
        alert, mail = await plan_one(seeded, "email", rule=rule)
        assert [await worker.process(mail["id"]) for _ in range(3)] == [
            "retrying",
            "retrying",
            "dead_lettered",
        ]
    finally:
        monkeypatch.delenv("SMTP_PORT")
        get_settings.cache_clear()
    assert await status_of(seeded, mail["id"]) == "dead_lettered"
    assert any(
        "Email delivery failed after 3 attempts" in t for t in await timeline_texts(seeded, alert["id"])
    )
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM deliveries WHERE alert_id = $1 AND channel = 'email'", alert["id"]
        )
        == 1
    )


async def test_slow_provider_times_out_after_three_seconds(seeded, worker, monkeypatch):
    class Slow:
        name = "sms"

        def render(self, alert, recipient, kind):
            return Message("sms", recipient.address, "x")

        async def send(self, msg, idempotency_key):
            await asyncio.sleep(30)

    monkeypatch.setattr("app.delivery.worker.SEND_TIMEOUT_S", 0.2)
    restore = registry.override("sms", Slow())
    try:
        _, sms = await plan_one(seeded, "sms")
        assert await worker.process(sms["id"]) == "retrying"
    finally:
        restore()
    row = await seeded.fetchrow("SELECT status, last_error FROM deliveries WHERE id = $1", sms["id"])
    assert row["status"] == "retrying" and "TimeoutError" in row["last_error"]


async def test_send_timeout_constant_matches_the_spec():
    from app.delivery.worker import SEND_TIMEOUT_S

    assert SEND_TIMEOUT_S == 3.0


async def test_crashing_handler_requeues_the_message(seeded, rabbit, worker, monkeypatch):
    _, ui = await plan_one(seeded, "ui")
    real = worker.process
    calls = []

    async def flaky(delivery_id):
        calls.append(delivery_id)
        if len(calls) == 1:
            raise RuntimeError("database hiccup")
        return await real(delivery_id)

    monkeypatch.setattr(worker, "process", flaky)
    monkeypatch.setattr("app.delivery.worker.NACK_DELAY_S", 0.01)
    await publish(seeded, rabbit, [ui])
    await wait_for(lambda: _is(seeded, ui["id"], "delivered"), what="requeued delivery")
    assert len(calls) == 2


async def test_malformed_messages_are_dropped(seeded, rabbit, worker):
    before = await queue_depth(rabbit, "deliveries.ui")
    await rabbit.publish("deliveries", "ui", b"not json")
    await rabbit.publish("deliveries", "ui", {"deliveryId": "nope"})
    await rabbit.publish("deliveries", "ui", {"other": 1})
    await asyncio.sleep(0.5)
    assert await queue_depth(rabbit, "deliveries.ui") == before


async def test_runbook_lookup_failure_does_not_break_email(seeded, worker, monkeypatch):
    import sys
    import types

    async def boom(conn, alert):
        raise RuntimeError("lookup broke")

    module = types.ModuleType("app.runbooks.lookup")
    module.runbook_for_alert = boom
    monkeypatch.setitem(sys.modules, "app.runbooks", types.ModuleType("app.runbooks"))
    monkeypatch.setitem(sys.modules, "app.runbooks.lookup", module)
    token = uuid.uuid4().hex[:10]
    rule = rule_def(hard=[PIOTR], channels={"critical": ["email"]})
    _, mail = await plan_one(seeded, "email", rule=rule, title=f"Runbook broken {token}")
    assert await worker.process(mail["id"]) == "delivered"
    await mailpit_search(f"subject:{token}")


async def test_runbook_steps_are_rendered_when_the_module_exists(seeded, worker, monkeypatch):
    import sys
    import types
    from types import SimpleNamespace

    async def found(conn, alert):
        return SimpleNamespace(
            name="settlement-timeout",
            steps=[{"title": "Check broker queue"}, SimpleNamespace(text="Retry workflow")],
        )

    module = types.ModuleType("app.runbooks.lookup")
    module.runbook_for_alert = found
    monkeypatch.setitem(sys.modules, "app.runbooks", types.ModuleType("app.runbooks"))
    monkeypatch.setitem(sys.modules, "app.runbooks.lookup", module)
    token = uuid.uuid4().hex[:10]
    rule = rule_def(hard=[PIOTR], channels={"critical": ["email"]})
    _, mail = await plan_one(seeded, "email", rule=rule, title=f"With runbook {token}")
    assert await worker.process(mail["id"]) == "delivered"
    message = (await mailpit_search(f"subject:{token}"))[0]
    async with httpx.AsyncClient() as client:
        detail = (await client.get(f"http://localhost:8025/api/v1/message/{message['ID']}")).json()
    assert "Check broker queue" in detail["HTML"] and "settlement-timeout" in detail["Text"]


# --- adapters -----------------------------------------------------------------------------------------


async def test_webhook_is_signed_with_hmac_sha256(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.delivery.channels.webhook.http_client", lambda: client)
    adapter = WebhookAdapter()
    from tests.delivery.test_render import view

    msg = adapter.render(view(), Recipient("hook", "http://hooks.test/in", secret="topsecret"), "soft")
    result = await adapter.send(msg, "key-1")
    assert result.status == "sent"
    request = seen[0]
    assert request.headers["X-Signature"] == sign("topsecret", request.content)
    assert request.headers["Idempotency-Key"] == "key-1"
    body = orjson.loads(request.content)
    assert body["alert"]["shortId"] == "A-1234" and body["idempotencyKey"] == "key-1"
    bad = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    monkeypatch.setattr("app.delivery.channels.webhook.http_client", lambda: bad)
    with pytest.raises(ChannelError):
        await adapter.send(msg, "key-2")
    with pytest.raises(ChannelError):
        await adapter.send(Message("webhook", None, ""), "key-3")


async def test_adapters_reject_recipients_without_an_address():
    for name in ("email", "sms", "phone"):
        with pytest.raises(ChannelError):
            await registry.get_adapter(name).send(Message(name, None, "x"), "k")
    assert (await registry.get_adapter("ui").send(Message("ui", None, "x"), "k")) == SendResult("delivered")


async def test_sms_adapter_surfaces_unreachable_gateway(monkeypatch):
    monkeypatch.setenv("MOCKGW_URL", "http://127.0.0.1:9")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(ChannelError):
            await registry.get_adapter("sms").send(Message("sms", "+48", "x"), "k")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_run_starts_consumers_and_stops_cleanly(seeded, rabbit, monkeypatch):
    from app.delivery.worker import run

    monkeypatch.setenv("METRICS_PORT", "0")
    stop = asyncio.Event()
    task = asyncio.create_task(run(stop))
    await asyncio.sleep(1.0)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 10)
