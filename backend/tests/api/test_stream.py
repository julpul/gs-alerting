"""GET /v1/stream: SSE fan-out over Redis, scope filtering, heartbeat, clean disconnect."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import orjson
import pytest

from app.api.routers import stream as stream_router
from app.core.metrics import SSE_CLIENTS
from app.core.sse import publish_sse, team_channel, user_channel
from tests.api import alerts_helpers as h


class SSEConnection:
    """Drives the ASGI app directly (httpx's ASGITransport buffers streaming bodies until they end)."""

    def __init__(self, app, path: str, query: dict[str, str], headers: dict[str, str] | None = None) -> None:
        self._app = app
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": urlencode(query).encode(),
            "root_path": "",
            "headers": [(b"host", b"test")]
            + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
        self._out: asyncio.Queue[dict] = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._buffer = ""

    async def _receive(self) -> dict:
        if not hasattr(self, "_sent_request"):
            self._sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        await self._out.put(message)

    async def open(self) -> SSEConnection:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        start = await self._next_message()
        assert start["type"] == "http.response.start"
        self.status = start["status"]
        self.headers = {k.decode(): v.decode() for k, v in start["headers"]}
        return self

    async def _next_message(self, wait: float = 3.0) -> dict:
        return await asyncio.wait_for(self._out.get(), wait)

    async def frames(self, count: int = 1, wait: float = 3.0) -> list[str]:
        """Read until `count` complete frames (blank-line separated) arrived."""
        frames: list[str] = []
        while len(frames) < count:
            while "\n\n" in self._buffer and len(frames) < count:
                frame, self._buffer = self._buffer.split("\n\n", 1)
                frames.append(frame)
            if len(frames) >= count:
                break
            message = await self._next_message(wait)
            if message["type"] == "http.response.body":
                self._buffer += message.get("body", b"").decode()
        return frames

    async def event(self, wait: float = 3.0) -> tuple[dict[str, str], dict]:
        """Next real event (skips the `retry:` preamble and heartbeat comments)."""
        while True:
            (frame,) = await self.frames(1, wait)
            if frame.startswith((": ", "retry:")):
                continue
            fields = dict(line.split(": ", 1) for line in frame.splitlines())
            return fields, orjson.loads(fields["data"])

    async def expect_silence(self, wait: float = 0.6) -> None:
        with pytest.raises(asyncio.TimeoutError):
            while True:
                (frame,) = await self.frames(1, wait)
                assert frame.startswith((": ", "retry:")), f"unexpected event: {frame}"

    async def close(self) -> None:
        self._disconnect.set()
        if self._task:
            await asyncio.wait_for(self._task, 3)


@pytest.fixture
async def connect(app, client, auth_headers):
    opened: list[SSEConnection] = []

    async def _connect(email: str, *, query: dict[str, str] | None = None, use_header: bool = False, **kw):
        headers = await auth_headers(email)
        token = headers["Authorization"].removeprefix("Bearer ")
        q = dict(query or {})
        extra = {}
        if use_header:
            extra["Authorization"] = headers["Authorization"]
        else:
            q["access_token"] = token
        conn = SSEConnection(app, "/v1/stream", q, {**extra, **kw.get("headers", {})})
        opened.append(conn)
        return await conn.open()

    yield _connect
    for conn in opened:
        await conn.close()


async def test_event_for_visible_team_is_delivered(connect):
    conn = await connect("anna@demo")
    assert conn.status == 200
    assert conn.headers["content-type"].startswith("text/event-stream")
    await publish_sse("alert.created", {"id": "A-1", "title": "boom"}, team_ids=[h.team("team-payments")])
    fields, body = await conn.event()
    assert fields["event"] == "alert.created"
    assert fields["id"] == "1"
    assert body["type"] == "alert.created"
    assert body["payload"] == {"id": "A-1", "title": "boom"}
    assert body["at"]
    await publish_sse("alert.updated", {"id": "A-1", "count": 2}, team_ids=[h.team("team-payments")])
    fields, body = await conn.event()
    assert (fields["event"], fields["id"], body["payload"]["count"]) == ("alert.updated", "2", 2)


async def test_event_for_invisible_team_is_not_delivered(connect):
    conn = await connect("anna@demo")
    await publish_sse("alert.created", {"id": "secret"}, team_ids=[h.team("team-settlements")])
    await conn.expect_silence()
    await publish_sse("alert.created", {"id": "visible"}, team_ids=[h.team("team-payments")])
    _, body = await conn.event()
    assert body["payload"]["id"] == "visible"


async def test_user_channel_and_bearer_header_auth(connect):
    conn = await connect("piotr@demo", use_header=True)
    await publish_sse("approval.requested", {"id": "ap1"}, user_ids=[h.user("piotr@demo")])
    _, body = await conn.event()
    assert body["type"] == "approval.requested"
    await publish_sse("approval.requested", {"id": "other"}, user_ids=[h.user("anna@demo")])
    await conn.expect_silence()


async def test_master_receives_every_team(connect):
    conn = await connect("master@demo")
    await publish_sse("alert.created", {"id": "s"}, team_ids=[h.team("team-settlements")])
    await publish_sse("alert.created", {"id": "p"}, team_ids=[h.team("team-payments")])
    ids = {(await conn.event())[1]["payload"]["id"] for _ in range(2)}
    assert ids == {"s", "p"}


async def test_team_filter(connect, client, auth_headers):
    conn = await connect("master@demo", query={"teamId": str(h.team("team-payments"))})
    await publish_sse("alert.created", {"id": "s"}, team_ids=[h.team("team-settlements")])
    await conn.expect_silence()
    await publish_sse("alert.created", {"id": "p"}, team_ids=[h.team("team-payments")])
    assert (await conn.event())[1]["payload"]["id"] == "p"

    headers = await auth_headers("anna@demo")
    forbidden = await client.get(
        "/v1/stream", params={"teamId": str(h.team("team-settlements"))}, headers=headers
    )
    assert forbidden.status_code == 403
    invalid = await client.get("/v1/stream", params={"teamId": "nope"}, headers=headers)
    assert invalid.status_code == 422


async def test_requires_valid_token(client):
    assert (await client.get("/v1/stream")).status_code == 401
    assert (await client.get("/v1/stream", params={"access_token": "garbage"})).status_code == 401


async def test_heartbeat_comment(connect, monkeypatch):
    monkeypatch.setattr(stream_router, "HEARTBEAT_SECONDS", 0.3)
    conn = await connect("anna@demo")
    frames = await conn.frames(2, wait=3)
    assert frames[0] == "retry: 3000"
    assert frames[1] == ": hb"


async def test_last_event_id_continues_numbering(connect):
    conn = await connect("anna@demo", headers={"Last-Event-ID": "41"})
    await publish_sse("alert.created", {"id": "x"}, team_ids=[h.team("team-payments")])
    fields, _ = await conn.event()
    assert fields["id"] == "42"


async def test_disconnect_unsubscribes_and_updates_gauge(connect, redis):
    before = SSE_CLIENTS._value.get()
    conn = await connect("anna@demo")
    assert SSE_CLIENTS._value.get() == before + 1
    channel = team_channel(h.team("team-payments"))
    assert dict(await redis.pubsub_numsub(channel))[channel] >= 1
    await conn.close()
    await asyncio.sleep(0.1)
    assert SSE_CLIENTS._value.get() == before
    assert (
        dict(await redis.pubsub_numsub(user_channel(h.user("anna@demo"))))[user_channel(h.user("anna@demo"))]
        == 0
    )


def test_format_event_variants():
    frame = stream_router.format_event(
        orjson.dumps({"type": "alert.acked", "payload": {"a": 1}, "at": "2026-01-01T00:00:00.000Z"}).decode(),
        7,
    )
    assert frame.startswith("event: alert.acked\nid: 7\ndata: ")
    assert frame.endswith("\n\n")
    assert orjson.loads(frame.split("data: ")[1]) == {
        "type": "alert.acked",
        "at": "2026-01-01T00:00:00.000Z",
        "payload": {"a": 1},
    }
    assert stream_router.format_event("not json", 1) is None
    assert stream_router.format_event('{"no": "type"}', 1) is None
