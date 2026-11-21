"""Simulator engine: baseline traffic, scenario playback, statistics."""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import generators as g
from .config import Settings
from .scenarios import INCIDENT_POOL, SCENARIOS, Context, Message, SimState, Step
from .transport import AmqpPublisher, HttpSender, SendResult

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

BASELINE_SOURCES: list[tuple[str, int]] = [
    ("pricing-logs", 35),
    ("workflow-broker", 28),
    ("trade-gateway", 20),
    ("prometheus", 15),
    ("pr-review-agent", 2),
]


def time_factor(local: datetime) -> float:
    """Traffic multiplier by local time: business hours are busier, nights and weekends quiet."""
    hour = local.hour
    if 8 <= hour < 18:
        factor = 1.5
    elif 6 <= hour < 8 or 18 <= hour < 22:
        factor = 0.8
    else:
        factor = 0.3
    return factor * (0.4 if local.weekday() >= 5 else 1.0)


class Stats:
    def __init__(self) -> None:
        self.sent: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.retries = 0
        self.amqp_published = 0
        self.amqp_fallbacks = 0
        self.scenarios_run: Counter[str] = Counter()
        self.last: deque[dict[str, Any]] = deque(maxlen=25)
        self.started_at = datetime.now(UTC)

    def record(self, msg: Message, result: SendResult, *, via: str, origin: str, now: datetime) -> None:
        if result.ok:
            self.sent[msg.source] += 1
        else:
            self.errors[msg.source] += 1
        self.retries += max(result.attempts - 1, 0)
        self.last.appendleft(
            {
                "at": g.iso(now),
                "source": msg.source,
                "via": via,
                "origin": origin,
                "status": result.status,
                "ok": result.ok,
                "attempts": result.attempts,
                "detail": result.detail,
            }
        )


class Simulator:
    def __init__(
        self,
        settings: Settings,
        sender: HttpSender,
        amqp: AmqpPublisher | None = None,
        *,
        rng: random.Random | None = None,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.sender = sender
        self.amqp = amqp
        self.rng = rng or random.Random()
        self.clock: Clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self.state = SimState()
        self.stats = Stats()
        self.base_rate = settings.base_rate
        self.paused = False
        self.auto_incidents = settings.auto_incidents
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loops: list[asyncio.Task[Any]] = []
        self._zone = ZoneInfo(settings.timezone)

    # ---- sending -------------------------------------------------------------------------------

    async def emit(self, msg: Message, origin: str = "baseline") -> SendResult:
        key = msg.key_override or self.settings.keys[msg.source]
        via = "webhook"
        if msg.kind == "raise":
            via = "raise"
            result = await self.sender.post("/v1/alerts/raise", msg.payload, {"X-Api-Key": key})
        elif msg.kind == "amqp" and self.amqp is not None:
            result = await self._publish_amqp(msg)
            via = "amqp" if result.ok else "webhook"
            if not result.ok:
                self.stats.amqp_fallbacks += 1
                result = await self.sender.post(f"/v1/ingest/{key}", msg.payload)
        else:
            result = await self.sender.post(f"/v1/ingest/{key}", msg.payload)
        self.stats.record(msg, result, via=via, origin=origin, now=self.clock())
        return result

    async def _publish_amqp(self, msg: Message) -> SendResult:
        assert self.amqp is not None
        try:
            await self.amqp.publish(msg.payload)
        except Exception as exc:
            return SendResult(False, None, 1, f"amqp: {type(exc).__name__}: {exc}"[:200])
        self.stats.amqp_published += 1
        return SendResult(True, None, 1, "published")

    # ---- scenarios -----------------------------------------------------------------------------

    def build_scenario(
        self, name: str, intensity: float = 1.0, params: dict[str, str] | None = None
    ) -> list[Step]:
        ctx = Context(
            rng=self.rng,
            now=self.clock(),
            intensity=max(intensity, 0.01),
            params=params or {},
            state=self.state,
            prometheus_format=self.settings.prometheus_format,
        )
        return SCENARIOS[name].build(ctx)

    async def play(self, steps: list[Step], origin: str) -> None:
        elapsed = 0.0
        for step in steps:
            wait = (step.delay - elapsed) / self.settings.scenario_speed
            elapsed = step.delay
            if wait > 0:
                await self._sleep(wait)
            await self.emit(step.message, origin=origin)

    def start_scenario(
        self,
        name: str,
        intensity: float = 1.0,
        params: dict[str, str] | None = None,
        origin: str | None = None,
    ) -> int:
        """Build and start a scenario in the background. Returns the number of steps."""
        if name == "resolve_all":
            self.cancel_scenarios()
        steps = self.build_scenario(name, intensity, params)
        self.stats.scenarios_run[name] += 1
        task = asyncio.create_task(self.play(steps, origin or f"scenario:{name}"))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return len(steps)

    def cancel_scenarios(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    @property
    def active_scenarios(self) -> int:
        return sum(1 for t in self._tasks if not t.done())

    # ---- baseline ------------------------------------------------------------------------------

    def current_rate(self) -> float:
        if self.paused:
            return 0.0
        return self.base_rate * time_factor(self.clock().astimezone(self._zone))

    def baseline_message(self) -> Message:
        names, weights = zip(*BASELINE_SOURCES, strict=True)
        source = self.rng.choices(names, weights)[0]
        now = self.clock()
        if source == "pricing-logs":
            return Message(source, g.pricing_log(self.rng, now))
        if source == "workflow-broker":
            return Message(source, g.noise_workflow_event(self.rng, now))
        if source == "trade-gateway":
            return Message(source, g.noise_trade_event(self.rng, now), kind="amqp")
        if source == "pr-review-agent":
            return Message(source, g.noise_raise(self.rng, now), kind="raise")
        alerts = [g.noise_prometheus_alert(self.rng, now)]
        return Message(source, g.prometheus_payloads(alerts, self.settings.prometheus_format)[0])

    async def baseline_loop(self) -> None:
        while True:
            rate = self.current_rate()
            if rate <= 0:
                await self._sleep(1.0)
                continue
            delay = self.rng.expovariate(rate)
            if delay > 2.0:  # memoryless: re-evaluate the rate every 2 s
                await self._sleep(2.0)
                continue
            await self._sleep(delay)
            await self.emit(self.baseline_message())

    async def incident_loop(self, tick: float = 5.0) -> None:
        while True:
            await self._sleep(tick)
            rate_per_s = self.settings.incidents_per_hour / 3600.0
            if not self.auto_incidents or rate_per_s <= 0:
                continue
            if self.rng.random() < 1 - math.exp(-rate_per_s * tick):
                self.start_random_incident()

    def start_random_incident(self) -> str:
        name, intensity, _ = self.rng.choices(INCIDENT_POOL, [w for _, _, w in INCIDENT_POOL])[0]
        self.start_scenario(name, intensity, {"resolve_after": "180"}, origin=f"incident:{name}")
        return name

    # ---- lifecycle -----------------------------------------------------------------------------

    def start_background(self) -> list[asyncio.Task[Any]]:
        """Start the baseline and random-incident loops."""
        self._loops = [asyncio.create_task(self.baseline_loop()), asyncio.create_task(self.incident_loop())]
        return self._loops

    async def shutdown(self) -> None:
        self.cancel_scenarios()
        for task in [*self._loops, *self._tasks]:
            task.cancel()
        for task in [*self._loops, *self._tasks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.sender.aclose()
        if self.amqp is not None:
            await self.amqp.aclose()

    def snapshot(self) -> dict[str, Any]:
        sources = sorted(set(self.settings.keys) | set(self.stats.sent) | set(self.stats.errors))
        return {
            "startedAt": g.iso(self.stats.started_at),
            "targetApiUrl": self.settings.target_api_url,
            "amqpEnabled": self.amqp is not None,
            "baseline": {
                "baseRate": self.base_rate,
                "effectiveRate": round(self.current_rate(), 3),
                "paused": self.paused,
                "autoIncidents": self.auto_incidents,
            },
            "sources": {s: {"sent": self.stats.sent[s], "errors": self.stats.errors[s]} for s in sources},
            "totals": {
                "sent": sum(self.stats.sent.values()),
                "errors": sum(self.stats.errors.values()),
                "retries": self.stats.retries,
                "amqpPublished": self.stats.amqp_published,
                "amqpFallbacks": self.stats.amqp_fallbacks,
            },
            "scenarios": {"running": self.active_scenarios, "runs": dict(self.stats.scenarios_run)},
            "last": list(self.stats.last),
        }
