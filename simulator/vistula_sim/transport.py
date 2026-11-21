"""Sending to the alerting API (HTTP with retries) and to the customer RabbitMQ (AMQP)."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aio_pika
import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_WAIT = 30.0


@dataclass(frozen=True)
class SendResult:
    ok: bool
    status: int | None
    attempts: int
    detail: str = ""


def retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0.0), MAX_WAIT)
    except ValueError:
        return None


class HttpSender:
    """POSTs JSON; retries 429/5xx and network errors with backoff, honouring Retry-After. Never raises."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 5,
        timeout: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._max_retries = max_retries
        self._sleep = sleep
        self._rng = rng or random.Random()

    def _backoff(self, attempt: int) -> float:
        return min(MAX_WAIT, 0.25 * 2**attempt) + self._rng.uniform(0, 0.1)

    async def post(self, path: str, payload: Any, headers: dict[str, str] | None = None) -> SendResult:
        attempts = 0
        detail = ""
        status: int | None = None
        while True:
            attempts += 1
            wait: float | None = None
            try:
                response = await self._client.post(path, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                status, detail = None, f"{type(exc).__name__}: {exc}"[:200]
            else:
                status = response.status_code
                if response.is_success:
                    return SendResult(True, status, attempts, response.text[:200])
                detail = response.text[:200]
                if status not in RETRY_STATUSES:
                    return SendResult(False, status, attempts, detail)
                wait = retry_after_seconds(response)
            if attempts > self._max_retries:
                return SendResult(False, status, attempts, detail)
            await self._sleep(wait if wait is not None else self._backoff(attempts - 1))

    async def aclose(self) -> None:
        await self._client.aclose()


class AmqpPublisher:
    """Publishes JSON messages to the customer's own queue (our connector consumes it)."""

    def __init__(
        self,
        url: str,
        queue: str,
        connect: Callable[[str], Awaitable[Any]] = aio_pika.connect_robust,
    ) -> None:
        self._url = url
        self._queue = queue
        self._connect = connect
        self._connection: Any = None
        self._channel: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_channel(self) -> Any:
        async with self._lock:
            if self._channel is None or self._channel.is_closed:
                self._connection = await self._connect(self._url)
                self._channel = await self._connection.channel()
                await self._channel.declare_queue(self._queue, durable=True)
            return self._channel

    async def publish(self, payload: dict[str, Any]) -> None:
        message = aio_pika.Message(
            body=json.dumps(payload).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        try:
            channel = await self._ensure_channel()
            await channel.default_exchange.publish(message, routing_key=self._queue)
        except Exception:
            self._channel = None
            raise

    async def aclose(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            self._channel = None
