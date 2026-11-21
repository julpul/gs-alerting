"""RabbitMQ source connector against the real broker (the test broker doubles as the customer's)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
import pytest_asyncio

from app.connectors.base import Backoff, SourceEvent, SourceSpec, make_publisher
from app.connectors.rabbitmq import RabbitMQSourceConnector, parse_message
from app.core.metrics import EVENTS_OUTCOME, INGESTED
from tests.connectors.conftest import eventually

FAST = dict(initial=0.05, maximum=0.2)


def spec(source: dict, queue: str | None, *, url: str | None = None, preset: str | None = None) -> SourceSpec:
    config = {k: v for k, v in {"queue": queue, "url": url}.items() if v}
    return SourceSpec(
        id=source["id"],
        name=source["name"],
        type="rabbitmq",
        preset=preset,
        default_severity="error",
        config=config,
    )


class Running:
    """A connector task that is cancelled when the test ends."""

    def __init__(self, connector: RabbitMQSourceConnector, publish) -> None:
        self.task = asyncio.create_task(connector.run(publish))

    async def stop(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest_asyncio.fixture
async def start(rabbit) -> AsyncIterator:
    runners: list[Running] = []

    def _start(source: dict, queue: str | None, *, publish=None, **kwargs) -> Running:
        s = spec(source, queue, **kwargs)
        connector = RabbitMQSourceConnector(s, backoff=Backoff(**FAST))
        running = Running(connector, publish or make_publisher(rabbit, s))
        runners.append(running)
        return running

    yield _start
    for running in runners:
        await running.stop()


def invalid_total() -> float:
    return EVENTS_OUTCOME.labels("invalid")._value.get()


# ---- parsing -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body", [b"not json", b"[1, 2]", b'"str"', b"", b"\xff\xfe", b"x" * (256 * 1024 + 1)]
)
def test_unusable_messages_do_not_parse(body):
    assert parse_message(body) is None


def test_json_object_parses():
    assert parse_message(b'{"a": 1}') == {"a": 1}


# ---- consume -> republish -> ack -------------------------------------------------------------------------


async def test_customer_message_lands_on_events_in_with_source_id_and_is_acked(
    start, make_preset_source, customer, tap
):
    source = await make_preset_source(type="rabbitmq")
    queue = await customer.new_queue()
    before = INGESTED.labels(source=source["name"])._value.get()
    running = start(source, queue)
    await customer.send(queue, {"meta": {"schema": "trade.rejected.v2"}, "payload": {"tradeId": "T-1"}})

    (envelope,) = await tap.wait_for(1, source)
    assert envelope["source_id"] == str(source["id"])
    assert envelope["raw"]["payload"]["tradeId"] == "T-1"
    assert envelope["typed"] is False and envelope["priority"] == 6  # source default severity: error
    assert INGESTED.labels(source=source["name"])._value.get() == before + 1

    await running.stop()  # unacked messages would return to the queue here
    assert await customer.depth(queue) == 0


async def test_many_messages_all_arrive_once(start, make_preset_source, customer, tap):
    source = await make_preset_source(type="rabbitmq")
    queue = await customer.new_queue()
    for i in range(250):
        await customer.send(queue, {"n": i})
    start(source, queue)
    envelopes = await tap.wait_for(250, source)
    assert sorted(e["raw"]["n"] for e in envelopes) == list(range(250))
    assert len({e["event_id"] for e in envelopes}) == 250


async def test_invalid_messages_are_counted_acked_and_not_requeued(start, make_preset_source, customer, tap):
    source = await make_preset_source(type="rabbitmq")
    queue = await customer.new_queue()
    before_invalid = invalid_total()
    before_ingested = INGESTED.labels(source=source["name"])._value.get()
    running = start(source, queue)
    for body in (b"{ not json", b"[1,2,3]", b'"just a string"'):
        await customer.send(queue, body)
    await customer.send(queue, {"ok": True})

    await tap.wait_for(1, source)
    await eventually(lambda: _flag(invalid_total() - before_invalid >= 3))
    assert invalid_total() - before_invalid == 3
    assert INGESTED.labels(source=source["name"])._value.get() - before_ingested == 4

    await running.stop()
    assert await customer.depth(queue) == 0
    assert len(tap.of(source)) == 1


async def test_preset_splits_alertmanager_payload_from_the_queue(start, make_preset_source, customer, tap):
    source = await make_preset_source(type="rabbitmq")
    queue = await customer.new_queue()
    start(source, queue, preset="alertmanager")
    alerts = [
        {"status": "firing", "labels": {"alertname": n, "service": "s", "severity": "warning"}} for n in "ABC"
    ]
    await customer.send(queue, {"receiver": "r", "status": "firing", "alerts": alerts})
    envelopes = await tap.wait_for(3, source)
    assert sorted(e["raw"]["alertname"] for e in envelopes) == ["A", "B", "C"]


async def test_message_id_makes_the_event_id_stable_across_redelivery(
    start, make_preset_source, customer, tap, rabbit
):
    source = await make_preset_source(type="rabbitmq")
    queue = await customer.new_queue()
    start(source, queue)
    await customer.send(queue, {"n": 1}, message_id="m-1")
    await customer.send(queue, {"n": 1}, message_id="m-1")
    envelopes = await tap.wait_for(2, source)
    expected = str(uuid5(NAMESPACE_URL, f"{source['id']}/m-1#0"))
    assert {e["event_id"] for e in envelopes} == {expected}


# ---- failures ---------------------------------------------------------------------------------------------


async def test_publish_failure_requeues_and_delivers_later(start, make_preset_source, customer, rabbit, tap):
    source = await make_preset_source(type="rabbitmq")
    queue = await customer.new_queue()
    real = make_publisher(rabbit, spec(source, queue))
    attempts = 0

    async def flaky(events: Sequence[SourceEvent]) -> int:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise ConnectionError("our broker is down")
        return await real(events)

    start(source, queue, publish=flaky)
    await customer.send(queue, {"n": 1})
    (envelope,) = await tap.wait_for(1, source)
    assert envelope["raw"] == {"n": 1} and attempts == 3
    await asyncio.sleep(0.3)
    assert len(tap.of(source)) == 1  # not duplicated by the retries


async def test_missing_queue_is_declared_when_the_connector_starts(start, make_preset_source, customer, tap):
    source = await make_preset_source(type="rabbitmq")
    queue = await customer.new_queue(declare=False)
    start(source, queue)
    await asyncio.sleep(0.5)
    await customer.send(queue, {"n": 7})
    (envelope,) = await tap.wait_for(1, source)
    assert envelope["raw"] == {"n": 7}


async def test_source_without_queue_does_not_start(start, make_preset_source):
    source = await make_preset_source(type="rabbitmq")
    running = start(source, None)
    await asyncio.wait_for(running.task, 2)  # returns immediately, nothing to consume


async def test_unreachable_broker_is_retried_not_fatal(start, make_preset_source):
    source = await make_preset_source(type="rabbitmq")
    running = start(source, "q", url="amqp://guest:guest@127.0.0.1:1/")
    await asyncio.sleep(0.4)
    assert not running.task.done()


# ---- reconnect -------------------------------------------------------------------------------------------


async def kill_connection(rabbit, name: str) -> None:
    """Close the broker side of the connection the connector opened (management API)."""
    async with httpx.AsyncClient(base_url="http://localhost:15672/api", auth=("guest", "guest")) as http:

        async def find() -> str | None:
            for conn in (await http.get("/connections")).json():
                if conn.get("user_provided_name") == name:
                    return conn["name"]
            return None

        target = await eventually(find, timeout=20)
        resp = await http.delete(f"/connections/{target.replace(' ', '%20')}")
        assert resp.status_code in (200, 204)


async def test_consumer_resumes_after_the_connection_is_closed(
    start, make_preset_source, customer, tap, rabbit
):
    source = await make_preset_source(type="rabbitmq")
    queue = await customer.new_queue()
    start(source, queue)
    await customer.send(queue, {"n": 1})
    await tap.wait_for(1, source)

    await kill_connection(rabbit, f"gs-connector:{source['name']}")
    await customer.send(queue, {"n": 2})
    envelopes = await tap.wait_for(2, source)
    assert sorted(e["raw"]["n"] for e in envelopes) == [1, 2]


async def test_consumer_resumes_after_its_queue_was_deleted(start, make_preset_source, customer, tap):
    source = await make_preset_source(type="rabbitmq")
    queue = await customer.new_queue()
    start(source, queue)
    await asyncio.sleep(0.5)
    await customer.channel.queue_delete(queue)
    await asyncio.sleep(0.5)
    await customer.send(queue, {"n": 3})  # published to the default exchange: dropped until redeclared
    await eventually(lambda: _declared(customer, queue))
    await customer.send(queue, {"n": 4})
    envelopes = await tap.wait_for(1, source)
    assert envelopes[-1]["raw"]["n"] == 4


async def _declared(customer, queue: str) -> bool:
    try:
        await customer.depth(queue)
    except Exception:
        return False
    return True


async def _flag(value: bool) -> bool:
    return value
