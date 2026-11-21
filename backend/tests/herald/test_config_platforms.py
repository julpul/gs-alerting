"""`/api/platforms`, `PUT /api/platforms/{id}` (token once), `/api/sources`, `POST /ingest/{platform}`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import orjson
import pytest_asyncio

from app.core.rabbit import Rabbit
from app.herald.platforms import TYPE_MAP
from tests.herald.config_helpers import ANNA, MASTER, eventually, sample

PLATFORM_KEYS = {
    "id", "name", "icon", "type", "topic", "path", "url", "managed", "needs_token",
    "firing", "acked", "resolved", "last_event_at",
}  # fmt: skip


class Tap:
    def __init__(self) -> None:
        self.items: list[dict] = []

    async def handler(self, message) -> None:
        async with message.process():
            self.items.append(orjson.loads(message.body))

    async def of(self, source_id: str, n: int = 1) -> list[dict]:
        async def mine() -> list[dict]:
            found = [e for e in self.items if e.get("source_id") == source_id]
            return found if len(found) >= n else []

        return await eventually(mine, timeout=20)


@pytest_asyncio.fixture
async def tap(rabbit) -> AsyncIterator[Tap]:
    consumer = Rabbit()
    await consumer.connect()
    t = Tap()
    await consumer.consume("events.in", t.handler, prefetch=200)
    yield t
    await consumer.close()


async def test_platforms_shape_and_counts(client, seeded):
    resp = await client.get("/api/platforms", headers=MASTER)
    assert resp.status_code == 200
    by_id = {p["id"]: p for p in resp.json()}
    assert {"workflow-broker", "prometheus", "trade-gateway", "custody"} <= set(by_id)
    for p in by_id.values():
        assert set(p) >= PLATFORM_KEYS
        assert p["needs_token"] is True and isinstance(p["firing"], int)
    assert (
        by_id["trade-gateway"]["type"] == "kafka"
        and by_id["trade-gateway"]["topic"] == "vistula.trade-gateway"
    )
    assert by_id["workflow-broker"]["path"] == "/ingest/workflow-broker"
    assert by_id["workflow-broker"]["icon"] == "workflow" and by_id["workflow-broker"]["managed"] == "file"
    anna = {p["id"] for p in (await client.get("/api/platforms", headers=ANNA)).json()}
    assert "prometheus" in anna and "trade-gateway" not in anna
    sources = (await client.get("/api/sources", headers=MASTER)).json()
    assert {s["id"] for s in sources} == set(by_id) and "firing" not in sources[0]


async def test_connect_platform_returns_token_once(client, seeded, tap):
    pid = f"ledger-{uuid4().hex[:6]}"
    resp = await client.put(
        f"/api/platforms/{pid}",
        json={"name": "Ledger"},
        headers={**MASTER, "X-Herald-Teams": "team-payments"},
    )
    assert resp.status_code == 200, resp.text
    made = resp.json()
    assert (
        made["id"] == pid
        and made["name"] == "Ledger"
        and made["managed"] == "ui"
        and made["type"] == "webhook"
    )
    assert made["token"].startswith("sk_") and made["url"].endswith(f"/ingest/{pid}")
    team = await seeded.fetchval(
        "SELECT t.slug FROM sources s JOIN teams t ON t.id = s.team_id WHERE s.name = $1", pid
    )
    assert team == "team-payments"

    listed = next(p for p in (await client.get("/api/platforms", headers=MASTER)).json() if p["id"] == pid)
    assert "token" not in listed and listed["name"] == "Ledger"
    again = await client.put(f"/api/platforms/{pid}", json={"name": "Ledger"}, headers=MASTER)
    assert again.status_code == 409
    assert (await client.put("/api/platforms/Bad Id", json={"name": "x"}, headers=MASTER)).status_code == 422

    # The token works on Herald's address, through our real ingest path.
    sent = await client.post(
        f"/ingest/{pid}",
        json={"type": "LEDGER_LAG", "severity": "WARN"},
        headers={"X-Herald-Token": made["token"]},
    )
    assert sent.status_code == 202 and sent.json()["accepted"] is True
    source_id = str(await seeded.fetchval("SELECT id FROM sources WHERE name = $1", pid))
    envelope = (await tap.of(source_id))[0]
    assert (
        envelope["raw"] == {"type": "LEDGER_LAG", "severity": "WARN"}
        and envelope["event_id"] == sent.json()["eventId"]
    )


async def test_connect_platform_team_rules(client):
    resp = await client.put(
        f"/api/platforms/p-{uuid4().hex[:6]}", json={"name": "x", "team": "team-rates"}, headers=ANNA
    )
    assert resp.status_code == 403
    own = await client.put(f"/api/platforms/p-{uuid4().hex[:6]}", json={"name": "Mine"}, headers=ANNA)
    assert own.status_code == 200 and own.json()["team"]


async def test_ingest_alias_auth_and_bodies(client, seeded, tap):
    source_id = str(await seeded.fetchval("SELECT id FROM sources WHERE name = 'workflow-broker'"))
    token = {"X-Herald-Token": "demo-key-workflow-broker"}
    assert (await client.post("/ingest/workflow-broker", json={"a": 1})).status_code == 401
    assert (
        await client.post("/ingest/workflow-broker", json={"a": 1}, headers={"X-Herald-Token": "nope"})
    ).status_code == 401
    wrong = await client.post("/ingest/prometheus", json={"a": 1}, headers=token)
    assert wrong.status_code == 401 and wrong.json()["detail"] == "bad or missing token"

    ok = await client.post(
        "/ingest/workflow-events", json=sample("workflow_failed"), headers=token
    )  # Herald's id
    assert ok.status_code == 202
    batch = await client.post("/ingest/workflow-broker", json=[{"n": 1}, {"n": 2}], headers=token)
    assert batch.json()["events"] == 2
    line = await client.post(
        "/ingest/workflow-broker", content=b"2026-10-23 WARN pricing slow", headers=token
    )
    assert line.status_code == 202
    in_path = await client.post("/ingest/demo-key-workflow-broker", json={"via": "path"})
    assert in_path.status_code == 202
    envelopes = await tap.of(source_id, 5)
    raws = [e["raw"] for e in envelopes]
    assert (
        {"message": "2026-10-23 WARN pricing slow"} in raws and {"via": "path"} in raws and {"n": 2} in raws
    )
    empty = await client.post("/ingest/workflow-broker", content=b"   ", headers=token)
    assert empty.status_code == 422


def test_type_map():
    assert TYPE_MAP == {"webhook": "webhook", "rabbitmq": "kafka"}
