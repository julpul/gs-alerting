"""`/api/sim/*`: send through the real ingest path + worker; Mailpit / mockgw proxies (mock transport)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.api.routers import herald_sim
from app.core.rabbit import Rabbit
from app.pipeline.rules.index import RuleIndex
from app.pipeline.rules.templates import render
from app.pipeline.worker import Worker
from tests.herald.config_helpers import MASTER, eventually, sample


async def _purge(rabbit: Rabbit) -> None:
    queue = await rabbit._channel().get_queue(rabbit.name("events.in"), ensure=False)
    await queue.purge()


@pytest_asyncio.fixture
async def real_worker(seeded, redis, rabbit) -> AsyncIterator[SimpleNamespace]:
    """The real worker (rule index, templates, delivery planner) consuming `events.in`."""
    await _purge(rabbit)
    consumer = Rabbit()
    await consumer.connect()
    index = RuleIndex()
    await index.load_all(seeded)
    worker = Worker(seeded, consumer, redis, index=index, render=render)
    await worker.start()
    try:
        yield SimpleNamespace(index=index)
    finally:
        await consumer.close()
        await worker.close()
        await _purge(rabbit)


async def test_samples(client):
    samples = (await client.get("/api/sim/samples", headers=MASTER)).json()
    assert {"workflow_failed", "trade_rejected", "pricing_log", "prometheus_high_error_rate"} <= set(samples)
    assert {"metric_breach", "metric_resolved", "workflow_succeeded", "unseen_margin_call"} <= set(samples)
    assert samples["metric_resolved"]["status"] == "resolved"
    assert (
        samples["workflow_succeeded"]["type"] == "WORKFLOW_SUCCEEDED"
        and "error" not in samples["workflow_succeeded"]
    )


async def test_send_raises_an_alert_through_the_worker(client, seeded, real_worker):
    event = sample("workflow_failed")
    event["workflowId"] = f"WF-HERALD-{uuid4().hex[:8]}"
    resp = await client.post(
        "/api/sim/send", json={"source": "workflow-events", "event": event}, headers=MASTER
    )
    assert resp.status_code == 202, resp.text
    assert (
        resp.json()["sent"] == 1
        and resp.json()["through"] == "webhook"
        and resp.json()["source"] == "workflow-broker"
    )

    async def alert():
        return await seeded.fetchrow(
            "SELECT a.title, a.status, r.name FROM alerts a JOIN rules r ON r.id = a.rule_id "
            "WHERE a.title LIKE '%validate-counterparty%' AND a.fields::text LIKE $1",
            f"%{event['workflowId']}%",
        )

    row = await eventually(alert, timeout=20)
    assert row["name"].startswith("Settlements") and row["status"] in ("open", "escalated")

    async def fired() -> bool:  # the recent stream is written after the alert's commit: poll, don't race
        recent = (await client.get("/api/events/recent?source=workflow-broker", headers=MASTER)).json()
        return any(r["raw"].get("workflowId") == event["workflowId"] and r["fired"] for r in recent)

    assert await eventually(fired, timeout=10)


async def test_fields_for_an_unseen_schema_after_one_event(client, real_worker):
    pid = f"margin-{uuid4().hex[:6]}"
    made = await client.put(
        f"/api/platforms/{pid}",
        json={"name": "Margin engine"},
        headers={**MASTER, "X-Herald-Teams": "team-settlements"},
    )
    assert made.status_code == 200
    assert (await client.get(f"/api/fields?source={pid}", headers=MASTER)).json() == []
    samples = (await client.get("/api/sim/samples", headers=MASTER)).json()
    sent = await client.post(
        "/api/sim/send", json={"source": pid, "event": samples["unseen_margin_call"]}, headers=MASTER
    )
    assert sent.status_code == 202

    async def fields():
        return (await client.get(f"/api/fields?source={pid}", headers=MASTER)).json()

    paths = {f["path"]: f for f in await eventually(fields, timeout=20)}
    assert paths["data.shortfall.amount"]["type"] == "float"
    assert paths["data.account.id"]["example"] == "ACC-44120" and paths["data.account.id"]["source"] == pid


async def test_send_errors_and_count(client, rabbit):
    assert (
        await client.post("/api/sim/send", json={"source": "nowhere", "event": {}}, headers=MASTER)
    ).status_code == 404
    await _purge(rabbit)
    burst = await client.post(
        "/api/sim/send",
        json={"source": "pricing-logs", "event": sample("pricing_log"), "count": 3},
        headers=MASTER,
    )
    assert burst.json()["sent"] == 3
    await _purge(rabbit)


def _mock(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_mail_proxy(client, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "ID": "m1",
                        "Subject": "[CRIT] A-1",
                        "To": [{"Address": "piotr@demo"}],
                        "Created": "2026-10-23T09:14:03Z",
                        "Snippet": "Settlement failed",
                    }
                ]
            },
        )

    monkeypatch.setattr(herald_sim, "http_client", _mock(handler))
    mail = (await client.get("/api/sim/mail", headers=MASTER)).json()
    assert seen["url"].endswith("/api/v1/messages?limit=20") and ":8025" in seen["url"]
    assert mail["page"] and mail["messages"] == [
        {
            "id": "m1",
            "subject": "[CRIT] A-1",
            "to": ["piotr@demo"],
            "at": "2026-10-23T09:14:03Z",
            "snippet": "Settlement failed",
        }
    ]
    monkeypatch.setattr(herald_sim, "http_client", _mock(lambda r: httpx.Response(503)))
    down = (await client.get("/api/sim/mail", headers=MASTER)).json()
    assert down == {"page": mail["page"], "messages": [], "unavailable": True}


async def test_phone_proxy(client, seeded, monkeypatch):
    phone = await seeded.fetchval("SELECT phone FROM users WHERE email = 'piotr@demo'")
    state = {
        "sms": [
            {
                "to": phone,
                "text": "[CRIT] A-7 Settlement failed. Reply ACK 7.",
                "at": "2026-10-23T09:14:03.000+00:00",
                "status": "queued",
            }
        ],
        "calls": [
            {"to": phone, "text": "Alert A-7", "at": "2026-10-23T09:14:04.000+00:00", "status": "ringing"}
        ],
        "replies": [{"from": phone, "text": "ACK 7", "at": "2026-10-23T09:15:00.000+00:00"}],
        "failures": {"sms": {"remainingSeconds": 30, "count": None}},
    }
    monkeypatch.setattr(herald_sim, "http_client", _mock(lambda r: httpx.Response(200, json=state)))
    body = (await client.get("/api/sim/phone", headers=MASTER)).json()
    assert body["down"] is True and body["page"]
    assert [m["kind"] for m in body["messages"]] == ["reply", "call", "sms"]
    assert all(m["person"] == "piotr" for m in body["messages"])
    assert isinstance(body["messages"][0]["at"], float)

    def broken(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(herald_sim, "http_client", _mock(broken))
    resp = await client.get("/api/sim/phone", headers=MASTER)
    assert resp.status_code == 502 and "did not answer" in resp.json()["detail"]


@pytest.mark.parametrize("down", [True, False])
async def test_gateway_toggle(client, monkeypatch, down):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import orjson

        calls.append((request.url.path, orjson.loads(request.content)))
        return httpx.Response(200, json={"failing": down})

    monkeypatch.setattr(herald_sim, "http_client", _mock(handler))
    resp = await client.post("/api/sim/gateway", json={"down": down}, headers=MASTER)
    assert resp.json() == {"down": down}
    assert [c[1]["channel"] for c in calls] == ["sms", "phone"]
    assert all(path == "/admin/fail" and (body["seconds"] > 0) == down for path, body in calls)


async def test_reply_goes_to_our_inbound_handler(client):
    resp = await client.post("/api/sim/reply", json={"person": "piotr", "text": "HELLO"}, headers=MASTER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["handled"] is True and body["reply"]
    ack = await client.post("/api/sim/reply", json={"person": "piotr", "text": "ACK 999999"}, headers=MASTER)
    assert ack.status_code == 200 and ack.json()["reply"]
    missing = await client.post("/api/sim/reply", json={"person": "ghost", "text": "ACK 1"}, headers=MASTER)
    assert missing.status_code == 404
