"""RabbitMQ (aio-pika): topology of spec 4.5, publisher confirms, batch publish, consume.

Names passed to `publish`/`consume` are *base* names; the `MQ_PREFIX` setting is applied inside
(`Rabbit.name`). Routing keys are never prefixed.

Topology (base names, routing key in brackets):
  events (direct)           -> events.in                         [events.in]
  deliveries (direct)       -> deliveries.{ui,email,sms,phone,webhook}   [channel]
  deliveries.retry (direct) -> deliveries.retry.{5s,30s,120s}   [5s|30s|120s]; TTL, then dead-letter
                               back to `deliveries`. To route the message back to its channel queue,
                               publish with header `CC: [<channel>]`: RabbitMQ re-uses the CC keys when
                               dead-lettering (see `retry_headers`).
  deliveries.dlq (fanout)   -> deliveries.dlq
  ai (direct)               -> ai.tasks                          [ai.tasks]
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aio_pika
import orjson
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractRobustConnection,
)

from app.config import get_settings

log = logging.getLogger(__name__)

EVENTS_EXCHANGE = "events"
EVENTS_QUEUE = "events.in"
DELIVERIES_EXCHANGE = "deliveries"
RETRY_EXCHANGE = "deliveries.retry"
DLQ_EXCHANGE = "deliveries.dlq"
DLQ_QUEUE = "deliveries.dlq"
AI_EXCHANGE = "ai"
AI_QUEUE = "ai.tasks"

CHANNELS = ("ui", "email", "sms", "phone", "webhook")
RETRY_DELAYS_MS = {"5s": 5_000, "30s": 30_000, "120s": 120_000}
MAX_PRIORITY = 10

Handler = Callable[[AbstractIncomingMessage], Awaitable[None]]


def retry_headers(channel: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Headers for publishing to `deliveries.retry` so the message returns to `deliveries.<channel>`."""
    return {**(extra or {}), "CC": [channel]}


class Rabbit:
    def __init__(self, url: str | None = None, prefix: str | None = None) -> None:
        settings = get_settings()
        self.url = url or settings.rabbitmq_url
        self.prefix = settings.mq_prefix if prefix is None else prefix
        self._connection: AbstractRobustConnection | None = None
        self._pub_channel: AbstractChannel | None = None
        self._exchanges: dict[str, AbstractExchange] = {}
        self._consume_channels: list[AbstractChannel] = []

    def name(self, base: str) -> str:
        return f"{self.prefix}{base}"

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self.url)
        self._pub_channel = await self._connection.channel(publisher_confirms=True)

    async def close(self) -> None:
        for ch in self._consume_channels:
            if not ch.is_closed:
                await ch.close()
        self._consume_channels.clear()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._connection = None
        self._pub_channel = None
        self._exchanges.clear()

    def _channel(self) -> AbstractChannel:
        if self._pub_channel is None:
            raise RuntimeError("Rabbit.connect() has not been called")
        return self._pub_channel

    async def _exchange(self, base: str) -> AbstractExchange:
        ex = self._exchanges.get(base)
        if ex is None:
            ex = await self._channel().get_exchange(self.name(base), ensure=False)
            self._exchanges[base] = ex
        return ex

    def _queue_specs(self) -> list[tuple[str, str, str, dict[str, Any]]]:
        """(queue, exchange, routing key, arguments) for every queue, base names."""
        prio = {"x-max-priority": MAX_PRIORITY}
        specs: list[tuple[str, str, str, dict[str, Any]]] = [
            (EVENTS_QUEUE, EVENTS_EXCHANGE, EVENTS_QUEUE, dict(prio)),
            (AI_QUEUE, AI_EXCHANGE, AI_QUEUE, {}),
            (DLQ_QUEUE, DLQ_EXCHANGE, "", {}),
        ]
        for ch in CHANNELS:
            specs.append(
                (
                    f"deliveries.{ch}",
                    DELIVERIES_EXCHANGE,
                    ch,
                    {**prio, "x-dead-letter-exchange": self.name(RETRY_EXCHANGE)},
                )
            )
        for label, ttl in RETRY_DELAYS_MS.items():
            specs.append(
                (
                    f"deliveries.retry.{label}",
                    RETRY_EXCHANGE,
                    label,
                    {"x-message-ttl": ttl, "x-dead-letter-exchange": self.name(DELIVERIES_EXCHANGE)},
                )
            )
        return specs

    async def declare_topology(self) -> None:
        ch = self._channel()
        types = {
            EVENTS_EXCHANGE: aio_pika.ExchangeType.DIRECT,
            DELIVERIES_EXCHANGE: aio_pika.ExchangeType.DIRECT,
            RETRY_EXCHANGE: aio_pika.ExchangeType.DIRECT,
            DLQ_EXCHANGE: aio_pika.ExchangeType.FANOUT,
            AI_EXCHANGE: aio_pika.ExchangeType.DIRECT,
        }
        for base, kind in types.items():
            self._exchanges[base] = await ch.declare_exchange(self.name(base), kind, durable=True)
        for queue, exchange, key, args in self._queue_specs():
            q = await ch.declare_queue(self.name(queue), durable=True, arguments=args or None)
            await q.bind(self._exchanges[exchange], routing_key=key)

    async def delete_topology(self) -> None:
        """Remove every queue/exchange of this prefix (used by test cleanup)."""
        ch = self._channel()
        for queue, *_ in self._queue_specs():
            await ch.queue_delete(self.name(queue))
        for base in (EVENTS_EXCHANGE, DELIVERIES_EXCHANGE, RETRY_EXCHANGE, DLQ_EXCHANGE, AI_EXCHANGE):
            await ch.exchange_delete(self.name(base))

    @staticmethod
    def _encode(body: dict | bytes) -> bytes:
        return body if isinstance(body, bytes) else orjson.dumps(body)

    def _message(self, body: dict | bytes, priority: int, headers: dict[str, Any] | None) -> aio_pika.Message:
        return aio_pika.Message(
            self._encode(body),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            priority=priority,
            headers=headers,
        )

    async def publish(
        self,
        exchange: str,
        routing_key: str,
        body: dict | bytes,
        *,
        priority: int = 0,
        headers: dict | None = None,
    ) -> None:
        """Publish a persistent message and await the broker's publisher confirm."""
        ex = await self._exchange(exchange)
        await ex.publish(self._message(body, priority, headers), routing_key=routing_key)

    async def publish_batch(
        self, exchange: str, routing_key: str, bodies: list[dict], *, priority: int = 0
    ) -> None:
        """Publish many messages concurrently on one channel; returns when all are confirmed."""
        ex = await self._exchange(exchange)
        await asyncio.gather(
            *(ex.publish(self._message(b, priority, None), routing_key=routing_key) for b in bodies)
        )

    async def consume(self, queue: str, handler: Handler, *, prefetch: int = 200) -> None:
        """Start consuming `queue` (base name). The handler must ack/nack/reject the message itself."""
        if self._connection is None:
            raise RuntimeError("Rabbit.connect() has not been called")
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=prefetch)
        self._consume_channels.append(channel)
        q = await channel.get_queue(self.name(queue), ensure=False)
        await q.consume(handler, no_ack=False)
