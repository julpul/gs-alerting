"""Ingest endpoints against the real app (Postgres + RabbitMQ)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from uuid import UUID

import orjson
import pytest
import pytest_asyncio

from app.api.routers.ingest import SourceKeyCache
from app.core.metrics import INGESTED
from app.core.rabbit import Rabbit
from app.core.security import hash_token
from tests.pipeline.conftest import eventually, purge_events_queue
from tests.pipeline.test_worker import WORKFLOW_RULE, alerts_for, sample

ISO_MS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class Tap:
    """Consumes `events.in` and collects (envelope, priority) pairs."""

    def __init__(self, rabbit: Rabbit) -> None:
        self.rabbit = rabbit
        self.items: list[tuple[dict, int]] = []

    async def handler(self, message) -> None:
        async with message.process():
            self.items.append((orjson.loads(message.body), message.priority))

    async def wait_for(self, n: int, source: dict) -> list[tuple[dict, int]]:
        """Envelopes of `source` only: other test modules may leave stray messages on `events.in`."""
        sid = str(source["id"])

        def mine() -> list[tuple[dict, int]]:
            return [item for item in self.items if item[0].get("source_id") == sid]

        await eventually(lambda: _ready(len(mine()) >= n), timeout=30)
        return mine()


async def _ready(flag: bool) -> bool:
    return flag


@pytest_asyncio.fixture
async def tap(rabbit) -> AsyncIterator[Tap]:
    await purge_events_queue(rabbit)
    consumer = Rabbit()
    await consumer.connect()
    t = Tap(consumer)
    await consumer.consume("events.in", t.handler, prefetch=200)
    yield t
    await consumer.close()


async def test_ingest_by_path_key_returns_202_and_publishes_envelope(client, make_source, tap):
    source = await make_source(default_severity="error")
    before = INGESTED.labels(source=source["name"])._value.get()
    resp = await client.post(f"/v1/ingest/{source['key']}", json=sample("workflow_failed"))
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"eventId", "receivedAt"}
    assert ISO_MS_Z.match(body["receivedAt"])
    assert UUID(body["eventId"]).version == 7

    ((envelope, priority),) = await tap.wait_for(1, source)
    assert envelope == {
        "event_id": body["eventId"],
        "source_id": str(source["id"]),
        "received_at": body["receivedAt"],
        "priority": 6,
        "typed": False,
        "raw": sample("workflow_failed"),
        "_meta": {},
    }
    assert priority == 6
    assert INGESTED.labels(source=source["name"])._value.get() == before + 1


async def test_ingest_with_api_key_header(client, make_source, tap):
    source = await make_source()
    resp = await client.post("/v1/ingest/_", json={"a": 1}, headers={"X-Api-Key": source["key"]})
    assert resp.status_code == 202
    ((envelope, priority),) = await tap.wait_for(1, source)
    assert envelope["source_id"] == str(source["id"]) and priority == 3


@pytest.mark.parametrize("headers", [{}, {"X-Api-Key": "nope"}])
async def test_underscore_key_needs_a_valid_header(client, headers):
    resp = await client.post("/v1/ingest/_", json={"a": 1}, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_unknown_key_is_401(client):
    resp = await client.post("/v1/ingest/ak_does_not_exist", json={"a": 1})
    assert resp.status_code == 401
    assert (await client.post("/v1/ingest/ak_does_not_exist/batch", json=[{"a": 1}])).status_code == 401


async def test_oversized_payload_is_413(client, make_source, tap):
    source = await make_source()
    big = {"blob": "x" * (256 * 1024 + 10)}
    resp = await client.post(f"/v1/ingest/{source['key']}", json=big)
    assert resp.status_code == 413
    assert resp.json()["code"] == "payload_too_large"
    ok = {"blob": "x" * (250 * 1024)}
    assert (await client.post(f"/v1/ingest/{source['key']}", json=ok)).status_code == 202
    assert len(await tap.wait_for(1, source)) == 1


@pytest.mark.parametrize("content", [b"not json", b"[1, 2]", b'"text"', b"123"])
async def test_non_object_body_is_422(client, make_source, content):
    source = await make_source()
    resp = await client.post(
        f"/v1/ingest/{source['key']}", content=content, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


async def test_batch_of_1000_is_accepted_and_all_consumed(client, make_source, tap):
    source = await make_source()
    events = [{"i": i, "type": "BATCH"} for i in range(1000)]
    resp = await client.post(f"/v1/ingest/{source['key']}/batch", json=events)
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 1000 and len(set(body["eventIds"])) == 1000
    items = await tap.wait_for(1000, source)
    assert {e["event_id"] for e, _ in items} == set(body["eventIds"])
    assert sorted(e["raw"]["i"] for e, _ in items) == list(range(1000))


async def test_batch_through_the_real_worker_is_fully_processed(
    client, make_source, make_rule, worker_env, outcomes
):
    await make_rule(WORKFLOW_RULE)
    source = await make_source()
    resp = await client.post(
        f"/v1/ingest/{source['key']}/batch", json=[{"n": i, "type": "OTHER"} for i in range(1000)]
    )
    assert resp.status_code == 202
    await eventually(lambda: _ready(outcomes["unrouted"] == 1000), timeout=60)
    queue = await worker_env.rabbit._channel().get_queue(worker_env.rabbit.name("events.in"), ensure=True)
    assert queue.declaration_result.message_count == 0


async def test_batch_limits_and_shape(client, make_source):
    source = await make_source()
    url = f"/v1/ingest/{source['key']}/batch"
    too_many = await client.post(url, json=[{"i": i} for i in range(1001)])
    assert too_many.status_code == 422 and "1000" in too_many.json()["message"]
    assert (await client.post(url, json={"not": "a list"})).status_code == 422
    assert (await client.post(url, json=[{"ok": 1}, "str"])).status_code == 422
    empty = await client.post(url, json=[])
    assert empty.status_code == 202 and empty.json() == {"accepted": 0, "eventIds": []}


async def test_rabbitmq_unavailable_is_503_with_retry_after(client, app, make_source):
    source = await make_source()
    dead = Rabbit()
    await dead.connect()
    await dead.close()
    live, app.state.rabbit = app.state.rabbit, dead
    try:
        single = await client.post(f"/v1/ingest/{source['key']}", json={"a": 1})
        batch = await client.post(f"/v1/ingest/{source['key']}/batch", json=[{"a": 1}, {"a": 2}])
        raised = await client.post(
            "/v1/alerts/raise",
            json={"type": "t", "severity": "info", "title": "x"},
            headers={"X-Api-Key": source["key"]},
        )
    finally:
        app.state.rabbit = live
    for resp in (single, batch, raised):
        assert resp.status_code == 503
        assert int(resp.headers["Retry-After"]) >= 1
        assert resp.json()["code"] == "service_unavailable"
    assert (await client.post(f"/v1/ingest/{source['key']}", json={"a": 1})).status_code == 202


async def test_typed_raise_with_api_key(client, make_source, tap):
    source = await make_source("team-security", name="agent-src")
    payload = sample("raise_secret_leaked")
    resp = await client.post("/v1/alerts/raise", json=payload, headers={"X-Api-Key": source["key"]})
    assert resp.status_code == 202
    ((envelope, priority),) = await tap.wait_for(1, source)
    assert priority == 9
    assert envelope["typed"] is True and envelope["source_id"] == str(source["id"])
    assert envelope["raw"] == payload
    meta = envelope["_meta"]
    assert meta["type"] == "security.secret_leaked" and meta["schema"] == "security.secret_leaked"
    assert meta["severity"] == "critical" and meta["title"] == payload["title"]
    assert meta["service"] == "payments-api" and meta["source"] == "agent-src"
    assert meta["timestamp"] == envelope["received_at"]


async def test_typed_raise_end_to_end_creates_alert(client, make_source, make_rule, worker_env, planner):
    rule = await make_rule(
        {
            "match": {
                "op": "and",
                "conditions": [{"field": "_type", "operator": "glob", "value": "security.*"}],
            },
            "presentation": {"fields": ["attributes.repo"]},
        },
        team="team-security",
    )
    source = await make_source("team-security")
    headers = {"X-Api-Key": source["key"]}
    payload = sample("raise_secret_leaked")
    for _ in range(3):
        assert (await client.post("/v1/alerts/raise", json=payload, headers=headers)).status_code == 202
    await eventually(lambda: _count_is(worker_env.pool, rule, 3))
    (alert,) = await alerts_for(worker_env.pool, rule)
    assert alert["severity"] == "critical" and alert["title"] == payload["title"]
    assert alert["flags"] == ["from_agent"]
    assert alert["fields"] == [{"label": "attributes.repo", "value": "payments-api"}]
    assert planner.reasons == ["created"]


async def _count_is(pool, rule, n: int) -> bool:
    rows = await alerts_for(pool, rule)
    return len(rows) == 1 and rows[0]["count"] == n


async def test_raise_validation_and_auth(client, make_source):
    source = await make_source()
    headers = {"X-Api-Key": source["key"]}
    assert (
        await client.post("/v1/alerts/raise", json={"type": "t", "severity": "info", "title": "x"})
    ).status_code == 401
    bad = await client.post(
        "/v1/alerts/raise", json={"type": "t", "severity": "loud", "title": "x"}, headers=headers
    )
    assert bad.status_code == 422
    missing = await client.post("/v1/alerts/raise", json={"type": "t"}, headers=headers)
    assert missing.status_code == 422


async def test_source_key_cache_ttl_negative_cache_and_lru(pool, make_source):
    cache = SourceKeyCache(ttl=0.2, negative_ttl=0.2, size=2)
    source = await make_source(name="cached-name")
    first = await cache.lookup(pool, source["key"])
    assert first["name"] == "cached-name"
    await pool.execute("UPDATE sources SET name = 'renamed' WHERE id = $1", source["id"])
    assert (await cache.lookup(pool, source["key"]))["name"] == "cached-name"  # served from cache
    await asyncio.sleep(0.25)
    assert (await cache.lookup(pool, source["key"]))["name"] == "renamed"  # expired, reloaded

    assert await cache.lookup(pool, "late-key") is None
    await pool.execute("UPDATE sources SET key_hash = $2 WHERE id = $1", source["id"], hash_token("late-key"))
    assert await cache.lookup(pool, "late-key") is None  # negative entry still fresh
    await asyncio.sleep(0.25)
    assert (await cache.lookup(pool, "late-key"))["id"] == source["id"]

    for i in range(5):
        await cache.lookup(pool, f"other-{i}")
    assert len(cache._items) == 2
    cache.clear()
    assert not cache._items


async def test_raise_with_bad_key_is_401_even_with_invalid_body(client):
    resp = await client.post("/v1/alerts/raise", json={}, headers={"X-Api-Key": "nope"})
    assert resp.status_code == 401
