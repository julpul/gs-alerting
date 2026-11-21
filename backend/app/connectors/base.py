"""Connector contract: a protocol adapter turns "something on the customer's side" into raw events.

A connector class implements `async def run(publish)`; the runner builds the `publish` callable for the
connector's source. `publish` splits (preset), wraps raw events into envelopes and publishes them to
`events.in` with a broker confirm, so a connector acks its own upstream only after `publish` returned.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.metrics import EVENTS_OUTCOME, INGESTED
from app.core.rabbit import EVENTS_EXCHANGE, EVENTS_QUEUE, Rabbit
from app.pipeline.envelope import build_envelope, priority_for
from app.pipeline.presets import split_events

log = logging.getLogger(__name__)

PUBLISH_TIMEOUT = 10.0


@dataclass(frozen=True)
class SourceSpec:
    """The part of a `sources` row a connector needs."""

    id: UUID
    name: str
    type: str
    preset: str | None
    default_severity: str
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def signature(self) -> tuple[Any, ...]:
        """Changes whenever a running connector must be restarted."""
        return (self.name, self.type, self.preset, self.default_severity, tuple(sorted(self.config.items())))


@dataclass(frozen=True)
class SourceEvent:
    """One raw event handed to `publish`. A `key` (e.g. the upstream message id) makes redelivered
    messages produce the same `event_id`, so the worker's idempotency check drops the repeat."""

    raw: dict[str, Any]
    key: str | None = None


Publish = Callable[[Sequence[SourceEvent]], Awaitable[int]]


class Connector(ABC):
    """One running ingestion loop for one source."""

    protocol: str

    def __init__(self, source: SourceSpec) -> None:
        self.source = source

    @abstractmethod
    async def run(self, publish: Publish) -> None:
        """Run until cancelled, reconnecting internally. A returned `publish(events)` means durable."""


def make_publisher(rabbit: Rabbit, source: SourceSpec) -> Publish:
    """`publish(events) -> count` for `source`: preset split, envelope, batch publish with confirm."""
    priority = priority_for(source.default_severity)

    async def publish(events: Sequence[SourceEvent]) -> int:
        envelopes = []
        for event in events:
            parts = split_events(source.preset, event.raw)
            for index, raw in enumerate(parts):
                event_id = uuid5(NAMESPACE_URL, f"{source.id}/{event.key}#{index}") if event.key else None
                envelopes.append(
                    build_envelope(source_id=source.id, raw=raw, priority=priority, event_id=event_id)
                )
        if envelopes:
            await asyncio.wait_for(
                rabbit.publish_batch(EVENTS_EXCHANGE, EVENTS_QUEUE, envelopes, priority=priority),
                PUBLISH_TIMEOUT,
            )
            INGESTED.labels(source=source.name).inc(len(envelopes))
        return len(envelopes)

    return publish


def count_invalid(source: SourceSpec) -> None:
    """A message we cannot turn into an event is still "ingested" (and acked), with outcome `invalid`."""
    INGESTED.labels(source=source.name).inc()
    EVENTS_OUTCOME.labels("invalid").inc()


class Backoff:
    """Exponential backoff with jitter: 1 s, 2 s, 4 s ... capped at `maximum`; `reset()` after a success."""

    def __init__(self, initial: float = 1.0, maximum: float = 30.0) -> None:
        self.initial = initial
        self.maximum = maximum
        self._current = initial

    def reset(self) -> None:
        self._current = self.initial

    def next(self) -> float:
        delay = self._current * random.uniform(0.8, 1.2)
        self._current = min(self._current * 2, self.maximum)
        return delay
