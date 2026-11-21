"""GET /v1/metrics/summary: Redis counters, queue depths, latency percentiles from Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.core.redis import rkey
from app.repositories import metrics as repo
from tests.api import alerts_helpers as h

# Latencies of the synthetic deliveries, in ms: p50 = 300, p99 = 400 + 0.96 * 100 = 496.
LATENCIES_MS = [100, 200, 300, 400, 500]


@pytest.fixture
async def stats(redis):
    """Set the Redis counter mirrors; cleaned afterwards."""
    keys = [rkey("stats", repo.INGESTED), rkey("stats", repo.OUTCOMES)]

    async def _set(ingested: dict[str, float], outcomes: dict[str, float]) -> None:
        await redis.delete(*keys)
        if ingested:
            await redis.hset(keys[0], mapping=ingested)
        if outcomes:
            await redis.hset(keys[1], mapping=outcomes)

    yield _set
    await redis.delete(*keys)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def synthetic(seeded):
    """Deliveries on team-security alerts (only root.security / master see them)."""
    now = datetime.now(UTC)
    alert = await h.make_alert(
        seeded,
        team_slug="team-security",
        domain_slug="security",
        with_refs=False,
        title="metrics",
        first_event_received_at=now - timedelta(milliseconds=50),
    )
    await seeded.execute("UPDATE alerts SET created_at = $2 WHERE id = $1", alert["id"], now)
    for i, ms in enumerate(LATENCIES_MS):
        await h.make_delivery(
            seeded,
            alert["id"],
            channel="sms" if i % 2 == 0 else "email",
            status="delivered",
            event_received_at=now - timedelta(milliseconds=ms),
            delivered_at=now,
        )
    old = now - timedelta(hours=1)
    await h.make_delivery(
        seeded,
        alert["id"],
        channel="phone",
        status="delivered",
        event_received_at=old - timedelta(seconds=9),
        delivered_at=old,
    )
    await h.make_delivery(seeded, alert["id"], channel="webhook", status="dead_lettered")
    return alert


async def test_summary_latency_percentiles_and_totals(client, auth_headers, synthetic, stats, monkeypatch):
    async def fake_depths():
        return {"events.in": 4, "deliveries.sms": 3, "deliveries.dlq": 2, "ai.tasks": 1}

    monkeypatch.setattr(repo, "queue_depths", fake_depths)
    await stats({"src-a": 10, "src-b": 5}, {"alerted_new": 8, "deduplicated": 2, "unrouted": 1})
    headers = await auth_headers("root.security@demo")

    resp = await client.get("/v1/metrics/summary", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["windowMinutes"] == 15
    assert body["latencyP50Ms"] == pytest.approx(300, abs=1)
    assert body["latencyP99Ms"] == pytest.approx(496, abs=1)
    overall_count = sum(c["count"] for c in body["deliveryLatencyMs"].values())
    assert overall_count == 5
    assert set(body["deliveryLatencyMs"]) == {"sms", "email"}
    sms = body["deliveryLatencyMs"]["sms"]  # 100, 300, 500
    assert sms["p50"] == pytest.approx(300, abs=1)
    assert sms["count"] == 3
    assert body["processingLatencyMs"]["p50"] == pytest.approx(50, abs=5)
    assert body["processingLatencyMs"]["count"] == 1
    assert body["notificationsDelivered"] == 6  # includes the old delivery (outside the window)
    assert body["inDeadLetter"] == 1 + 2  # dead_lettered delivery + DLQ depth
    assert body["queueDepth"] == 4 + 3 + 1
    assert body["queues"]["deliveries.dlq"] == 2
    assert body["eventsIngested"] == 15
    assert body["outcomes"] == {"alerted_new": 8, "deduplicated": 2, "unrouted": 1}
    assert body["lost"] == 0  # 15 - 11 outcomes - 4 still in events.in


async def test_summary_window_changes_percentiles(client, auth_headers, synthetic, stats):
    await stats({}, {})
    resp = await client.get(
        "/v1/metrics/summary", params={"windowMinutes": 120}, headers=await auth_headers("root.security@demo")
    )
    body = resp.json()
    assert body["windowMinutes"] == 120
    assert sum(c["count"] for c in body["deliveryLatencyMs"].values()) == 6
    assert body["deliveryLatencyMs"]["phone"]["p50"] == pytest.approx(9000, abs=1)
    assert body["eventsIngested"] == 0
    assert body["lost"] == 0


async def test_summary_is_scoped_for_deliveries(client, auth_headers, synthetic, stats):
    await stats({}, {})
    anna = (await client.get("/v1/metrics/summary", headers=await auth_headers("anna@demo"))).json()
    assert "sms" not in anna["deliveryLatencyMs"] or anna["deliveryLatencyMs"]["sms"]["count"] == 0
    master = (await client.get("/v1/metrics/summary", headers=await auth_headers("master@demo"))).json()
    assert master["notificationsDelivered"] >= 6


async def test_lost_counts_unaccounted_events(client, auth_headers, stats, monkeypatch):
    async def fake_depths():
        return {"events.in": 2}

    monkeypatch.setattr(repo, "queue_depths", fake_depths)
    await stats({"s": 20}, {"alerted_new": 10, "deduplicated": 5})
    body = (await client.get("/v1/metrics/summary", headers=await auth_headers("master@demo"))).json()
    assert body["lost"] == 3  # 20 - 15 - 2 in flight


async def test_lost_is_unknown_without_outcome_counters(client, auth_headers, stats):
    await stats({"s": 20}, {})
    body = (await client.get("/v1/metrics/summary", headers=await auth_headers("master@demo"))).json()
    assert body["eventsIngested"] == 20
    assert body["lost"] is None


async def test_rabbitmq_unreachable_gives_null_depths(client, auth_headers, stats, monkeypatch):
    monkeypatch.setenv("RABBITMQ_MGMT_URL", "http://127.0.0.1:1")
    await stats({}, {})
    body = (await client.get("/v1/metrics/summary", headers=await auth_headers("master@demo"))).json()
    assert body["queueDepth"] is None
    assert body["queues"] is None


async def test_rabbitmq_management_api_depths_are_real(client, auth_headers, stats):
    await stats({}, {})
    body = (await client.get("/v1/metrics/summary", headers=await auth_headers("master@demo"))).json()
    assert isinstance(body["queueDepth"], int)
    assert "events.in" in body["queues"]


async def test_summary_validation_and_auth(client, auth_headers):
    assert (await client.get("/v1/metrics/summary")).status_code == 401
    resp = await client.get(
        "/v1/metrics/summary", params={"windowMinutes": 0}, headers=await auth_headers("anna@demo")
    )
    assert resp.status_code == 422


def test_events_per_second_sampling():
    repo._samples.clear()
    assert repo.events_per_second(100, now=1000.0) == 0.0
    assert repo.events_per_second(100, now=1000.1) == 0.0  # too close to the first sample
    assert repo.events_per_second(160, now=1002.0) == pytest.approx(30.0)
    assert repo.events_per_second(460, now=1009.0) == pytest.approx((460 - 100) / 9.0)
    # samples older than the window drop out
    assert repo.events_per_second(500, now=1030.0) == 0.0
    repo._samples.clear()
