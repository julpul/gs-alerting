from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from vistula_sim.config import Settings
from vistula_sim.engine import Simulator
from vistula_sim.transport import HttpSender

NOW = datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)  # a Monday, business hours in Warsaw


@dataclass
class Recorder:
    """Captures requests made through httpx.MockTransport and answers 202."""

    requests: list[httpx.Request] = field(default_factory=list)
    status: int = 202

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"eventId": "e1"})

    def bodies(self) -> list[Any]:
        import json

        return [json.loads(r.content) for r in self.requests]


class FakeAmqp:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self.fail = fail
        self.closed = False

    async def publish(self, payload: dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.published.append(payload)

    async def aclose(self) -> None:
        self.closed = True


async def no_sleep(_: float) -> None:
    return None


def make_sim(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    amqp: FakeAmqp | None = None,
    sleeps: list[float] | None = None,
    **settings: Any,
) -> Simulator:
    cfg = Settings(scenario_speed=1000.0, **settings)
    client = httpx.AsyncClient(base_url=cfg.target_api_url, transport=httpx.MockTransport(handler))

    async def record_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)
        await asyncio.sleep(0)

    sender = HttpSender(cfg.target_api_url, client=client, max_retries=3, sleep=record_sleep)
    return Simulator(
        cfg,
        sender,
        amqp,
        rng=random.Random(7),
        clock=lambda: NOW,
        sleep=record_sleep,  # type: ignore[arg-type]
    )


@pytest.fixture
def rng() -> random.Random:
    return random.Random(42)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()
