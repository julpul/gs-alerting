"""RuleIndex hot reload over Redis pub/sub, the 30 s safety net, and recovery from a failed reload."""

from __future__ import annotations

import asyncio
import time

import orjson
import pytest

from app.core.redis import rkey
from app.pipeline.rules import index as index_module
from app.pipeline.rules.index import RuleIndex

from .conftest import cond, definition


async def until(predicate, within: float = 2.0) -> float:
    start = time.perf_counter()
    while time.perf_counter() - start < within:
        if predicate():
            return time.perf_counter() - start
        await asyncio.sleep(0.02)
    raise AssertionError("not reached in time")


@pytest.fixture
async def running(seeded, monkeypatch):
    monkeypatch.setattr(index_module, "POLL_SECONDS", 0.1)
    index = RuleIndex()
    await index.load_all(seeded)
    task = await index.start_hot_reload(seeded)
    yield index
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def publish(redis, **payload):
    await redis.publish(rkey("rules:changed"), orjson.dumps(payload))


async def test_message_reloads_the_rule_within_a_second(running, seeded, insert_rule, redis, unique):
    rule_id = await insert_rule(definition(cond("x", "eq", unique)))
    await publish(redis, ruleId=str(rule_id), version=1)
    assert await until(lambda: running.get(rule_id) is not None) < 1.0

    await seeded.execute(
        "UPDATE rules SET definition = $2, version = 2 WHERE id = $1",
        rule_id,
        definition(cond("x", "eq", "new")),
    )
    await publish(redis, ruleId=str(rule_id), version=2)
    assert await until(lambda: running.get(rule_id).rule.version == 2) < 1.0

    await seeded.execute("DELETE FROM rules WHERE id = $1", rule_id)
    await publish(redis, ruleId=str(rule_id), version=None)
    assert await until(lambda: running.get(rule_id) is None) < 1.0


async def test_unparseable_message_triggers_a_full_reload(running, insert_rule, redis, unique):
    rule_id = await insert_rule(definition(cond("x", "eq", unique)))
    await redis.publish(rkey("rules:changed"), "garbage")
    await until(lambda: running.get(rule_id) is not None)
    await redis.publish(rkey("rules:changed"), orjson.dumps({"ruleId": "not-a-uuid"}))
    await until(lambda: running.get(rule_id) is not None)


async def test_periodic_full_reload_is_the_safety_net(seeded, insert_rule, monkeypatch, unique):
    monkeypatch.setattr(index_module, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(index_module, "FULL_RELOAD_SECONDS", 0.2)
    index = RuleIndex()
    task = await index.start_hot_reload(seeded)
    try:
        rule_id = await insert_rule(definition(cond("x", "eq", unique)))  # no pub/sub message at all
        await until(lambda: index.get(rule_id) is not None)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_listener_survives_a_failing_reload(seeded, insert_rule, redis, monkeypatch, unique):
    monkeypatch.setattr(index_module, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(index_module, "RECONNECT_SECONDS", 0.05)
    index = RuleIndex()
    real = index.reload_rule
    failures = []

    async def flaky(pool, rule_id):
        if not failures:
            failures.append(rule_id)
            raise ConnectionError("db hiccup")
        await real(pool, rule_id)

    monkeypatch.setattr(index, "reload_rule", flaky)
    task = await index.start_hot_reload(seeded)
    try:
        rule_id = await insert_rule(definition(cond("x", "eq", unique)))
        await publish(redis, ruleId=str(rule_id), version=1)
        await until(lambda: bool(failures))
        # the recovery path does a full reload, so the rule shows up even though the message failed
        await until(lambda: index.get(rule_id) is not None)
        other = await insert_rule(definition(cond("x", "eq", unique + "b")))
        await publish(redis, ruleId=str(other), version=1)
        await until(lambda: index.get(other) is not None)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
