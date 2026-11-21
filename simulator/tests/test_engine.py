from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import httpx

from tests.conftest import FakeAmqp, Recorder, make_sim
from vistula_sim.config import Settings
from vistula_sim.engine import time_factor
from vistula_sim.scenarios import Message


def test_time_factor_business_hours_busier_than_night_and_weekend():
    monday_noon = datetime(2026, 11, 23, 12, tzinfo=UTC)
    monday_night = datetime(2026, 11, 23, 3, tzinfo=UTC)
    saturday_noon = datetime(2026, 11, 28, 12, tzinfo=UTC)
    evening = datetime(2026, 11, 23, 20, tzinfo=UTC)
    assert time_factor(monday_noon) > time_factor(evening) > time_factor(monday_night)
    assert time_factor(saturday_noon) < time_factor(monday_noon)


async def test_emit_ingest_uses_source_key_in_path(recorder: Recorder):
    sim = make_sim(recorder.handler)
    result = await sim.emit(Message("prometheus", {"a": 1}))
    assert result.ok
    assert recorder.requests[0].url.path == "/v1/ingest/demo-key-prometheus"
    assert sim.stats.sent["prometheus"] == 1


async def test_emit_raise_uses_api_key_header(recorder: Recorder):
    sim = make_sim(recorder.handler)
    await sim.emit(Message("pr-review-agent", {"type": "security.secret_leaked"}, kind="raise"))
    request = recorder.requests[0]
    assert request.url.path == "/v1/alerts/raise"
    assert request.headers["x-api-key"] == "demo-key-pr-review-agent"


async def test_emit_amqp_publishes_to_queue_not_http(recorder: Recorder):
    amqp = FakeAmqp()
    sim = make_sim(recorder.handler, amqp=amqp)
    result = await sim.emit(Message("trade-gateway", {"x": 1}, kind="amqp"))
    assert result.ok and amqp.published == [{"x": 1}] and recorder.requests == []
    assert sim.snapshot()["totals"]["amqpPublished"] == 1


async def test_emit_amqp_falls_back_to_webhook_when_broker_down(recorder: Recorder):
    sim = make_sim(recorder.handler, amqp=FakeAmqp(fail=True))
    result = await sim.emit(Message("trade-gateway", {"x": 1}, kind="amqp"))
    assert result.ok
    assert recorder.requests[0].url.path == "/v1/ingest/demo-key-trade-gateway"
    assert sim.snapshot()["totals"]["amqpFallbacks"] == 1
    assert sim.stats.last[0]["via"] == "webhook"


async def test_emit_amqp_without_publisher_uses_webhook(recorder: Recorder):
    sim = make_sim(recorder.handler)
    await sim.emit(Message("trade-gateway", {"x": 1}, kind="amqp"))
    assert recorder.requests[0].url.path == "/v1/ingest/demo-key-trade-gateway"


async def test_key_override_is_used(recorder: Recorder):
    sim = make_sim(recorder.handler)
    await sim.emit(Message("custody", {}, key_override="my-key"))
    assert recorder.requests[0].url.path == "/v1/ingest/my-key"


async def test_api_down_is_counted_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    sim = make_sim(handler)
    result = await sim.emit(Message("prometheus", {}))
    assert not result.ok
    snap = sim.snapshot()
    assert snap["totals"]["errors"] == 1 and snap["sources"]["prometheus"]["errors"] == 1
    assert snap["totals"]["retries"] == 3 and snap["last"][0]["ok"] is False


async def test_503_then_success_through_simulator():
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "1"})
        return httpx.Response(202, json={})

    sleeps: list[float] = []
    sim = make_sim(handler, sleeps=sleeps)
    result = await sim.emit(Message("pricing-logs", {}))
    assert result.ok and result.attempts == 2 and sleeps == [1.0]
    assert sim.snapshot()["totals"]["retries"] == 1


async def test_storm_scenario_sends_500_identical_events(recorder: Recorder):
    sim = make_sim(recorder.handler)
    sim.start_scenario("storm_500")
    await asyncio.gather(*sim._tasks)
    bodies = recorder.bodies()
    assert len(bodies) == 500
    assert len({(b["workflowId"], b["step"], b["error"]["code"]) for b in bodies}) == 1
    assert sim.snapshot()["sources"]["workflow-broker"]["sent"] == 500
    assert sim.stats.scenarios_run["storm_500"] == 1


async def test_scenario_waits_scaled_delays():
    sleeps: list[float] = []
    sim = make_sim(Recorder().handler, sleeps=sleeps)
    sim.start_scenario("settlement_outage", intensity=1.0)
    await asyncio.gather(*sim._tasks)
    assert sleeps and all(s == 4.0 / 1000.0 for s in sleeps)


async def test_resolve_all_cancels_running_scenarios_and_resolves(recorder: Recorder):
    sim = make_sim(recorder.handler)
    sim.start_scenario("payments_error_spike", params={"resolve_after": "0"})
    await asyncio.gather(*sim._tasks)
    recorder.requests.clear()

    blocker = asyncio.create_task(asyncio.sleep(60))
    sim._tasks.add(blocker)
    blocker.add_done_callback(sim._tasks.discard)
    sim.start_scenario("resolve_all")
    await asyncio.gather(blocker, return_exceptions=True)
    assert blocker.cancelled()
    await asyncio.gather(*sim._tasks)
    bodies = recorder.bodies()
    assert bodies[0]["status"] == "resolved"


async def test_baseline_messages_are_valid_and_cover_all_sources(recorder: Recorder):
    sim = make_sim(recorder.handler)
    seen = {sim.baseline_message().source for _ in range(2000)}
    assert seen == {"pricing-logs", "workflow-broker", "trade-gateway", "prometheus", "pr-review-agent"}


async def test_baseline_loop_emits_at_configured_rate_and_pause_stops_it():
    rec = Recorder()
    sim = make_sim(rec.handler, base_rate=1000.0)
    sleeps = 0

    async def sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 20:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    sim._sleep = sleep
    with contextlib.suppress(asyncio.CancelledError):
        await sim.baseline_loop()
    assert len(rec.requests) >= 15

    sim.paused = True
    assert sim.current_rate() == 0.0


async def test_baseline_loop_idles_when_rate_is_zero():
    rec = Recorder()
    sim = make_sim(rec.handler, base_rate=0.0)
    calls: list[float] = []

    async def sleep(s: float) -> None:
        calls.append(s)
        if len(calls) >= 3:
            raise asyncio.CancelledError

    sim._sleep = sleep
    with contextlib.suppress(asyncio.CancelledError):
        await sim.baseline_loop()
    assert calls == [1.0, 1.0, 1.0] and rec.requests == []


async def test_incident_loop_starts_incidents_when_enabled():
    rec = Recorder()
    sim = make_sim(rec.handler, incidents_per_hour=3_600_000.0)
    ticks = 0

    async def sleep(_: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > 3:
            raise asyncio.CancelledError

    sim._sleep = sleep
    with contextlib.suppress(asyncio.CancelledError):
        await sim.incident_loop()
    assert sum(sim.stats.scenarios_run.values()) >= 2
    await asyncio.gather(*sim._tasks, return_exceptions=True)


async def test_incident_loop_respects_disabled_flag():
    sim = make_sim(Recorder().handler, auto_incidents=False, incidents_per_hour=3_600_000.0)
    ticks = 0

    async def sleep(_: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > 3:
            raise asyncio.CancelledError

    sim._sleep = sleep
    with contextlib.suppress(asyncio.CancelledError):
        await sim.incident_loop()
    assert not sim.stats.scenarios_run


async def test_start_background_and_shutdown_close_everything():
    amqp = FakeAmqp()
    sim = make_sim(Recorder().handler, amqp=amqp, base_rate=0.0)
    loops = sim.start_background()
    await asyncio.sleep(0)
    await sim.shutdown()
    assert all(t.done() for t in loops) and amqp.closed


def test_settings_from_env_parses_and_validates():
    s = Settings.from_env(
        {
            "TARGET_API_URL": "http://api:8080/",
            "BASE_RATE": "5",
            "CUSTOMER_AMQP_URL": "amqp://g:g@rmq/",
            "AUTO_INCIDENTS": "false",
            "PROMETHEUS_FORMAT": "bogus",
            "SOURCE_KEY_PROMETHEUS": "k1",
            "PORT": "9000",
        }
    )
    assert s.target_api_url == "http://api:8080" and s.base_rate == 5.0 and s.port == 9000
    assert s.customer_amqp_url == "amqp://g:g@rmq/" and s.auto_incidents is False
    assert s.prometheus_format == "alertmanager" and s.keys["prometheus"] == "k1"
    assert s.keys["pricing-logs"] == "demo-key-pricing-logs"
    defaults = Settings.from_env({})
    assert defaults.port == 8095 and defaults.customer_amqp_url is None and defaults.auto_incidents is True
