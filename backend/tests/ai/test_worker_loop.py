"""AI worker role: queue registration and the resolution sweep."""

from __future__ import annotations

import asyncio

from app.ai import worker as worker_mod
from app.ai.worker import AIWorker
from tests.ai.conftest import add_resolution
from tests.api.alerts_helpers import make_alert


async def test_worker_registers_its_consumer_and_sweeps_unindexed_resolutions(seeded, monkeypatch):
    consumed = []

    class Rabbit:
        async def consume(self, queue, handler, *, prefetch):
            consumed.append((queue, prefetch))

    await AIWorker(seeded, Rabbit()).start()
    assert consumed == [("ai.tasks", worker_mod.PREFETCH)]

    old = await make_alert(seeded, status="resolved", title="Sweep me please")
    resolution = await add_resolution(seeded, old, "Swept note about the clearing queue.")
    stop = asyncio.Event()
    monkeypatch.setattr(worker_mod, "SWEEP_INTERVAL_S", 0.05)
    task = asyncio.create_task(worker_mod._sweep_loop(seeded, stop))
    for _ in range(100):
        if await seeded.fetchval("SELECT 1 FROM knowledge_docs WHERE resolution_id = $1", resolution):
            break
        await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 5)
    kind = await seeded.fetchval("SELECT kind FROM knowledge_docs WHERE resolution_id = $1", resolution)
    assert kind == "resolution_note"
