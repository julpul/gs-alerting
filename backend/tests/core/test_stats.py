import asyncio

from app.core import metrics, stats


async def test_flush_mirrors_deltas_to_redis(redis):
    base = await stats.flush({})
    metrics.EVENTS_OUTCOME.labels(outcome="unrouted").inc(3)
    metrics.INGESTED.labels(source="s1").inc(2)
    base = await stats.flush(base)
    totals = await stats.read_stats()
    assert totals["alerting_events_outcome_total"]["unrouted"] >= 3
    assert totals["alerting_ingested_total"]["s1"] >= 2
    before = totals["alerting_ingested_total"]["s1"]
    await stats.flush(base)  # no new increments -> no change
    assert (await stats.read_stats())["alerting_ingested_total"]["s1"] == before


async def test_mirror_loop_stops(redis):
    stop = asyncio.Event()
    task = asyncio.create_task(stats.run_stats_mirror(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 3)


def test_metrics_server_port_conflict_is_tolerated():
    stats.start_metrics_server(0)
