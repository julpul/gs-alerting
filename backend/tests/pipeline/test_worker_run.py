"""The worker role entry point: start-up wiring, graceful stop, stop during start-up."""

from __future__ import annotations

import asyncio

import pytest

from app.pipeline import worker as worker_module
from app.pipeline.envelope import build_envelope
from tests.pipeline.conftest import alerts_for, eventually, fake_render, purge_events_queue
from tests.pipeline.test_worker import WORKFLOW_RULE, sample


@pytest.fixture
def wired(index, monkeypatch):
    monkeypatch.setattr(worker_module, "_default_index", lambda: index)
    monkeypatch.setattr(worker_module, "_default_render", lambda: fake_render)
    monkeypatch.setenv("METRICS_PORT", "0")


async def test_run_loads_rules_consumes_and_stops_cleanly(
    wired, index, make_source, make_rule, planner, seeded, rabbit, monkeypatch
):
    monkeypatch.setattr("app.pipeline.alert_writer._default_planner", lambda: planner)
    rule = await make_rule(WORKFLOW_RULE)
    source = await make_source()
    await purge_events_queue(rabbit)
    stop = asyncio.Event()
    task = asyncio.create_task(worker_module.run(stop))
    try:
        await eventually(lambda: _flag(getattr(index, "reloading", None) is not None))
        envelope = build_envelope(source_id=source["id"], raw=sample("workflow_failed"), priority=6)
        await rabbit.publish("events", "events.in", envelope, priority=6)
        (alert,) = await eventually(lambda: alerts_for(seeded, rule))
        assert alert["count"] == 1 and planner.reasons == ["created"]
    finally:
        stop.set()
    await asyncio.wait_for(task, 10)
    assert index.reloading.cancelled()


async def test_stop_during_startup_exits_quickly(wired):
    stop = asyncio.Event()
    task = asyncio.create_task(worker_module.run(stop))
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)


async def _flag(value: bool) -> bool:
    return value
