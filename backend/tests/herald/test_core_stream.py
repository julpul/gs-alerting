"""/api/stream: our Redis SSE events re-framed in Herald's feed format, scoped like /v1/stream."""

from __future__ import annotations

import pytest

from app.api.routers import herald_stream
from app.core.metrics import SSE_CLIENTS
from app.core.redis import get_redis
from app.core.sse import publish_sse, team_channel
from tests.api import alerts_helpers as h
from tests.api.test_stream import SSEConnection

from .core_helpers import alert, as_person


@pytest.fixture
async def connect(app):
    opened: list[SSEConnection] = []

    async def _connect(person: str, query: dict[str, str] | None = None, headers: dict | None = None):
        conn = SSEConnection(app, "/api/stream", query or {}, {**as_person(person), **(headers or {})})
        opened.append(conn)
        return await conn.open()

    yield _connect
    for conn in opened:
        await conn.close()


async def test_alert_events_become_herald_frames(connect):
    conn = await connect("piotr")
    assert conn.status == 200 and conn.headers["content-type"].startswith("text/event-stream")
    await publish_sse(
        "alert.acked",
        {"alertId": "x", "shortId": "A-77", "status": "acknowledged"},
        team_ids=[h.team("team-settlements")],
    )
    fields, body = await conn.event()
    assert fields["event"] == "alert" and fields["id"] == "1"
    assert body == {
        "id": 1,
        "owner": "team:team-settlements",
        "kind": "alert",
        "alert_id": 77,
        "type": "alert.acked",
    }


async def test_other_teams_are_silent_and_owner_is_checked(connect, client):
    conn = await connect("piotr")
    await publish_sse("alert.created", {"shortId": "A-1"}, team_ids=[h.team("team-payments")])
    await conn.expect_silence()
    r = await client.get("/api/stream", params={"owner": "team:team-payments"}, headers=as_person("piotr"))
    assert r.status_code == 403 and r.json() == {"detail": "not your team"}

    only_pay = await connect("master", query={"owner": "team:team-payments"})
    await publish_sse("alert.created", {"shortId": "A-2"}, team_ids=[h.team("team-settlements")])
    await only_pay.expect_silence()


async def test_delivery_and_approval_events_resolve_the_alert(connect, seeded):
    row = await alert(seeded, title="Herald stream delivery")
    conn = await connect("anna", headers={"Last-Event-ID": "41"})
    await publish_sse(
        "delivery.status",
        {"id": "d1", "alertId": str(row["id"]), "status": "delivered"},
        team_ids=[h.team("team-payments")],
    )
    fields, body = await conn.event()
    assert fields["event"] == "delivery" and fields["id"] == "42"
    assert body["alert_id"] == row["short_id"] and body["owner"] == "team:team-payments"

    await publish_sse("approval.requested", {"id": "ap", "shortId": 5}, user_ids=[h.user("anna@demo")])
    fields, body = await conn.event()
    assert fields["event"] == "approval" and body["alert_id"] is None and body["owner"] is None

    await get_redis().publish(team_channel(h.team("team-payments")), "not json")
    await publish_sse("suggestion.ready", {"alertId": str(row["id"])}, team_ids=[h.team("team-payments")])
    fields, body = await conn.event()
    assert fields["event"] == "alert" and body["type"] == "suggestion.ready" and fields["id"] == "44"


async def test_api_action_reaches_the_stream(connect, client, seeded):
    row = await alert(seeded, title="Herald stream ack")
    conn = await connect("anna")
    assert (
        await client.post(f"/api/alerts/{row['short_id']}/ack", headers=as_person("anna"))
    ).status_code == 200
    _, body = await conn.event()
    assert body["kind"] == "alert" and body["alert_id"] == row["short_id"] and body["type"] == "alert.acked"


async def test_heartbeat_and_clean_disconnect(connect, monkeypatch):
    monkeypatch.setattr(herald_stream, "HEARTBEAT_SECONDS", 0.3)
    before = SSE_CLIENTS._value.get()
    conn = await connect("anna")
    frames = await conn.frames(2, wait=3)
    assert frames == [": connected", ": keep-alive"]
    assert SSE_CLIENTS._value.get() == before + 1
    await conn.close()
    assert SSE_CLIENTS._value.get() == before


def test_kind_of():
    assert herald_stream.kind_of("alert.escalated") == "alert"
    assert herald_stream.kind_of("delivery.status") == "delivery"
    assert herald_stream.kind_of("approval.decided") == "approval"
    assert herald_stream.kind_of("org.changed") == "org"
