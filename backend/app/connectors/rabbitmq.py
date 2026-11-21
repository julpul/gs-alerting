"""RabbitMQ source connector (spec 5.2): consume a customer queue, republish into our `events.in`.

Delivery guarantee: a customer message is acked only after our broker confirmed the republished envelope
(at-least-once; a message with a `message_id` keeps the same `event_id` when redelivered, so the worker
drops the repeat). Messages that are not a JSON object (or are over the event size limit) count as
`invalid` and are acked, never requeued.

Connection loss (broker restart, network, a closed channel) ends the session; the supervisor loop
reconnects with exponential backoff and starts a fresh consumer. Nothing acked on a dead channel is
trusted: unacked messages are redelivered by the broker to the new consumer.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

import aio_pika
import orjson
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractIncomingMessage, AbstractQueue
from aiormq.exceptions import ChannelNotFoundEntity

from app.config import get_settings
from app.connectors.base import Backoff, Connector, Publish, SourceEvent, SourceSpec, count_invalid
from app.pipeline.envelope import MAX_EVENT_BYTES

log = logging.getLogger(__name__)

PREFETCH = 200
BATCH_MAX = 100
LINGER_SECONDS = 0.005
WATCH_SECONDS = 1.0
QUEUE_CHECK_SECONDS = 5.0


def parse_message(body: bytes) -> dict[str, Any] | None:
    """The JSON object in a customer message, or None when it is not usable."""
    if len(body) > MAX_EVENT_BYTES:
        return None
    try:
        raw = orjson.loads(body)
    except orjson.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


class RabbitMQSourceConnector(Connector):
    protocol = "rabbitmq"

    def __init__(self, source: SourceSpec, *, backoff: Backoff | None = None) -> None:
        super().__init__(source)
        self.url: str = source.config.get("url") or get_settings().rabbitmq_url
        self.queue: str | None = source.config.get("queue")
        self.backoff = backoff or Backoff()

    async def run(self, publish: Publish) -> None:
        if not self.queue:
            log.error("source %s has no config.queue; connector not started", self.source.name)
            return
        while True:
            try:
                await self._session(publish)
                log.warning("source %s: connection to %s lost", self.source.name, self.queue)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("source %s: consumer failed (%r)", self.source.name, exc)
            await asyncio.sleep(self.backoff.next())

    async def _open(self, connection: AbstractConnection) -> tuple[AbstractChannel, AbstractQueue]:
        """A channel with prefetch + the queue; a missing queue is declared durable."""
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=PREFETCH)
        try:
            return channel, await channel.declare_queue(self.queue, passive=True)
        except ChannelNotFoundEntity:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=PREFETCH)
            return channel, await channel.declare_queue(self.queue, durable=True)

    async def _session(self, publish: Publish) -> None:
        """One connection's lifetime: returns when the connection or channel is gone."""
        connection = await aio_pika.connect(
            self.url, client_properties={"connection_name": f"gs-connector:{self.source.name}"}
        )
        lost = asyncio.Event()
        inbox: asyncio.Queue[AbstractIncomingMessage] = asyncio.Queue()
        flusher: asyncio.Task | None = None
        try:
            connection.close_callbacks.add(lambda *_: lost.set())
            channel, queue = await self._open(connection)
            channel.close_callbacks.add(lambda *_: lost.set())
            flusher = asyncio.create_task(self._flush_loop(inbox, publish), name=f"flush-{self.source.name}")
            await queue.consume(inbox.put, no_ack=False)
            log.info("source %s: consuming %s", self.source.name, self.queue)
            self.backoff.reset()
            checked = asyncio.get_running_loop().time()
            while not lost.is_set() and not flusher.done():
                with suppress(TimeoutError):
                    await asyncio.wait_for(lost.wait(), WATCH_SECONDS)
                if asyncio.get_running_loop().time() - checked >= QUEUE_CHECK_SECONDS:
                    # the broker cancels our consumer silently when the queue is deleted
                    await channel.declare_queue(self.queue, passive=True)
                    checked = asyncio.get_running_loop().time()
            if flusher.done():
                flusher.result()
        finally:
            if flusher is not None:
                flusher.cancel()
                await asyncio.gather(flusher, return_exceptions=True)
            with suppress(Exception):
                await connection.close()

    async def _flush_loop(self, inbox: asyncio.Queue[AbstractIncomingMessage], publish: Publish) -> None:
        while True:
            batch = [await inbox.get()]
            await asyncio.sleep(LINGER_SECONDS)
            while len(batch) < BATCH_MAX and not inbox.empty():
                batch.append(inbox.get_nowait())
            await self._handle(batch, publish)

    async def _handle(self, batch: list[AbstractIncomingMessage], publish: Publish) -> None:
        events: list[SourceEvent] = []
        accepted: list[AbstractIncomingMessage] = []
        for message in batch:
            raw = parse_message(message.body)
            if raw is None:
                count_invalid(self.source)
                await _settle(message.ack())
                continue
            events.append(SourceEvent(raw, key=message.message_id))
            accepted.append(message)
        if not events:
            return
        try:
            await publish(events)
        except Exception as exc:
            log.warning(
                "source %s: publish to events.in failed (%r); requeueing %d",
                self.source.name,
                exc,
                len(accepted),
            )
            await asyncio.gather(*(_settle(m.nack(requeue=True)) for m in accepted))
            await asyncio.sleep(self.backoff.next())
            return
        self.backoff.reset()
        await asyncio.gather(*(_settle(m.ack()) for m in accepted))


async def _settle(operation: Any) -> None:
    """Ack/nack on a channel that may just have died: the broker redelivers, so a failure is not fatal."""
    try:
        await operation
    except Exception as exc:
        log.debug("could not settle message: %r", exc)
