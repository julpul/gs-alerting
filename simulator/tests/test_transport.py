from __future__ import annotations

import json
import random
from typing import Any

import httpx
import pytest

from vistula_sim.transport import AmqpPublisher, HttpSender, retry_after_seconds


def make_sender(handler, sleeps: list[float], max_retries: int = 3) -> HttpSender:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(handler))
    return HttpSender(
        "http://api.test", client=client, max_retries=max_retries, sleep=sleep, rng=random.Random(0)
    )


async def test_success_first_try_sends_json_and_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"eventId": "x"})

    sleeps: list[float] = []
    sender = make_sender(handler, sleeps)
    result = await sender.post("/v1/alerts/raise", {"a": 1}, {"X-Api-Key": "k"})
    assert result.ok and result.status == 202 and result.attempts == 1 and sleeps == []
    assert seen[0].url.path == "/v1/alerts/raise" and seen[0].headers["x-api-key"] == "k"
    assert json.loads(seen[0].content) == {"a": 1}


async def test_503_is_retried_and_retry_after_is_honoured():
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, headers={"Retry-After": "2"}, json={"code": "unavailable"})
        return httpx.Response(202, json={})

    sleeps: list[float] = []
    result = await make_sender(handler, sleeps).post("/v1/ingest/k", {})
    assert result.ok and result.attempts == 3
    assert sleeps == [2.0, 2.0]


async def test_429_without_retry_after_uses_exponential_backoff():
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] < 3 else 202)

    sleeps: list[float] = []
    result = await make_sender(handler, sleeps).post("/x", {})
    assert result.ok and len(sleeps) == 2
    assert 0.25 <= sleeps[0] < 0.4 and 0.5 <= sleeps[1] < 0.65


async def test_gives_up_after_max_retries_and_never_raises():
    sleeps: list[float] = []
    result = await make_sender(lambda _: httpx.Response(503, text="down"), sleeps, max_retries=2).post(
        "/x", {}
    )
    assert not result.ok and result.status == 503 and result.attempts == 3 and len(sleeps) == 2


async def test_client_errors_are_not_retried():
    sleeps: list[float] = []
    result = await make_sender(lambda _: httpx.Response(401, text="bad key"), sleeps).post("/x", {})
    assert not result.ok and result.status == 401 and result.attempts == 1 and sleeps == []
    assert result.detail == "bad key"


async def test_connection_errors_are_retried_then_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    sleeps: list[float] = []
    result = await make_sender(handler, sleeps, max_retries=2).post("/x", {})
    assert not result.ok and result.status is None and result.attempts == 3
    assert "ConnectError" in result.detail


def test_retry_after_parsing_caps_and_ignores_garbage():
    assert retry_after_seconds(httpx.Response(503, headers={"Retry-After": "7"})) == 7.0
    assert retry_after_seconds(httpx.Response(503, headers={"Retry-After": "9999"})) == 30.0
    assert retry_after_seconds(httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2026"})) is None
    assert retry_after_seconds(httpx.Response(503)) is None


class FakeChannel:
    def __init__(self) -> None:
        self.declared: list[tuple[str, bool]] = []
        self.sent: list[tuple[Any, str]] = []
        self.is_closed = False
        outer = self

        class Exchange:
            async def publish(self, message: Any, routing_key: str) -> None:
                outer.sent.append((message, routing_key))

        self.default_exchange = Exchange()

    async def declare_queue(self, name: str, durable: bool) -> None:
        self.declared.append((name, durable))


class FakeConnection:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel
        self.closed = False

    async def channel(self) -> FakeChannel:
        return self._channel

    async def close(self) -> None:
        self.closed = True


async def test_amqp_declares_durable_queue_and_publishes_persistent_json():
    channel = FakeChannel()
    connection = FakeConnection(channel)
    connects: list[str] = []

    async def connect(url: str) -> FakeConnection:
        connects.append(url)
        return connection

    publisher = AmqpPublisher("amqp://x", "vistula.trade-gateway", connect=connect)
    await publisher.publish({"meta": {"schema": "trade.rejected.v2"}})
    await publisher.publish({"n": 2})
    assert connects == ["amqp://x"]  # connection reused
    assert channel.declared == [("vistula.trade-gateway", True)]
    message, routing_key = channel.sent[0]
    assert routing_key == "vistula.trade-gateway" and message.content_type == "application/json"
    assert json.loads(message.body) == {"meta": {"schema": "trade.rejected.v2"}}
    assert int(message.delivery_mode) == 2
    await publisher.aclose()
    assert connection.closed


async def test_amqp_connect_failure_propagates_and_next_publish_reconnects():
    attempts = {"n": 0}
    channel = FakeChannel()

    async def connect(_: str) -> FakeConnection:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("down")
        return FakeConnection(channel)

    publisher = AmqpPublisher("amqp://x", "q", connect=connect)
    with pytest.raises(ConnectionError):
        await publisher.publish({})
    await publisher.publish({"ok": True})
    assert len(channel.sent) == 1
