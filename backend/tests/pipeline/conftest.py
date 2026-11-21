"""Fixtures for the pipeline tests: a fake rule set (the real one is P2-RULES), a recording delivery planner
(the real one is P2-DELIVERY), source/rule rows inserted with SQL, and a real worker on a real queue."""

from __future__ import annotations

import asyncio
import fnmatch
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from app.core.ids import stable_id
from app.core.metrics import EVENTS_OUTCOME
from app.core.rabbit import Rabbit
from app.core.security import hash_token
from app.pipeline.envelope import build_envelope
from app.pipeline.fields import EventView
from app.pipeline.worker import Worker

TEMPLATE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def to_ns(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{snake(k): to_ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [to_ns(v) for v in value]
    return value


def check(cond: dict, ev: EventView) -> bool:
    """Tiny evaluator for the openapi ConditionGroup subset the tests use."""
    if "conditions" in cond:
        results = (check(c, ev) for c in cond["conditions"])
        return all(results) if cond["op"] == "and" else any(results)
    actual, wanted, op = ev.get(cond["field"]), cond.get("value"), cond["operator"]
    match op:
        case "eq":
            return actual == wanted
        case "neq":
            return actual != wanted
        case "gt":
            return actual is not None and actual > wanted
        case "glob":
            return isinstance(actual, str) and fnmatch.fnmatch(actual, wanted)
        case "in":
            return actual in wanted
        case "contains":
            return actual is not None and wanted in actual
        case "exists":
            return (actual is not None) == wanted
    raise AssertionError(f"unsupported operator {op}")


class FakeCompiledRule:
    def __init__(self, rule_id: UUID, team_id: UUID, definition: dict) -> None:
        self.rule = to_ns(definition)
        self.rule.id = rule_id
        self.rule.team_id = team_id
        self.definition = definition

    def matches(self, ev: EventView) -> bool:
        return check(self.definition["match"], ev)


class FakeRuleIndex:
    def __init__(self) -> None:
        self.rules: list[FakeCompiledRule] = []

    def candidates(self, ev: EventView) -> list[FakeCompiledRule]:
        return list(self.rules)

    async def load_all(self, pool) -> None:
        self.loaded = True

    async def start_hot_reload(self, pool) -> asyncio.Task:
        self.reloading = asyncio.create_task(asyncio.sleep(3600))
        return self.reloading


def fake_render(template: str, ev: EventView, *, html: bool = False, max_len: int = 500) -> str:
    def value(match: re.Match) -> str:
        found = ev.get(match.group(1))
        return "" if found is None else str(found)

    return TEMPLATE.sub(value, template)[:max_len]


class RecordingPlanner:
    """Stands in for `routing.plan_deliveries`; optionally writes an outbox row like the real one."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self.outbox_routing_key: str | None = None

    async def __call__(self, conn, alert, rule, *, reason, level=1):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append({"alert": alert, "rule": rule, "reason": reason, "level": level})
        if self.outbox_routing_key:
            from app.core.outbox import add_outbox

            await add_outbox(conn, "deliveries", self.outbox_routing_key, {"alert_id": str(alert["id"])})
        return []

    @property
    def reasons(self) -> list[str]:
        return [c["reason"] for c in self.calls]


async def eventually(check_fn: Callable[[], Awaitable[Any]], timeout: float = 20.0) -> Any:  # noqa: ASYNC109
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await check_fn()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s (last value: {value!r})")
        await asyncio.sleep(0.05)


def outcome_count(label: str) -> float:
    return EVENTS_OUTCOME.labels(label)._value.get()


class OutcomeDeltas:
    """Counter deltas since the test started (the Prometheus counters are process-wide)."""

    def __init__(self) -> None:
        self.start = {
            o: outcome_count(o) for o in ("alerted_new", "deduplicated", "grouped", "unrouted", "invalid")
        }

    def __getitem__(self, label: str) -> int:
        return int(outcome_count(label) - self.start[label])


@pytest.fixture
def outcomes() -> OutcomeDeltas:
    return OutcomeDeltas()


@pytest_asyncio.fixture
async def make_source(seeded) -> Callable[..., Awaitable[dict]]:
    pool = seeded

    async def factory(
        team: str = "team-settlements",
        *,
        name: str | None = None,
        mapping: dict | None = None,
        mapping_status: str | None = None,
        default_severity: str = "warning",
    ) -> dict:
        key = f"test-key-{uuid4().hex}"
        team_id = stable_id("team", team)
        domain_id = await pool.fetchval(
            "SELECT domain_id FROM team_domains WHERE team_id = $1 ORDER BY domain_id LIMIT 1", team_id
        )
        source_id = uuid4()
        name = name or f"src-{source_id.hex[:8]}"
        await pool.execute(
            "INSERT INTO sources (id, name, team_id, domain_id, type, key_prefix, key_hash, mapping, "
            "mapping_status, default_severity) VALUES ($1, $2, $3, $4, 'webhook', $5, $6, $7, $8, $9)",
            source_id,
            name,
            team_id,
            domain_id,
            key[:9],
            hash_token(key),
            mapping,
            mapping_status,
            default_severity,
        )
        return {"id": source_id, "name": name, "key": key, "team_id": team_id, "domain_id": domain_id}

    return factory


@pytest_asyncio.fixture
async def make_rule(seeded, index) -> Callable[..., Awaitable[FakeCompiledRule]]:
    """Insert a `rules` row (definition shaped like the openapi Rule), add its compiled fake to the index."""
    pool = seeded

    async def factory(definition: dict, team: str = "team-settlements") -> FakeCompiledRule:
        rule_id, team_id = uuid4(), stable_id("team", team)
        definition = {"name": f"rule-{rule_id.hex[:8]}", "teamId": team, "routing": {}, **definition}
        await pool.execute(
            "INSERT INTO rules (id, team_id, name, definition) VALUES ($1, $2, $3, $4)",
            rule_id,
            team_id,
            definition["name"],
            definition,
        )
        compiled = FakeCompiledRule(rule_id, team_id, definition)
        index.rules.append(compiled)
        return compiled

    return factory


@pytest.fixture
def index() -> FakeRuleIndex:
    return FakeRuleIndex()


@pytest.fixture
def planner() -> RecordingPlanner:
    return RecordingPlanner()


async def purge_events_queue(rabbit: Rabbit) -> None:
    queue = await rabbit._channel().get_queue(rabbit.name("events.in"), ensure=False)
    await queue.purge()


@pytest_asyncio.fixture
async def worker_env(seeded, redis, rabbit, index, planner) -> AsyncIterator[SimpleNamespace]:
    """A started worker (own RabbitMQ connection) on an empty `events.in`; `publish(...)` feeds it."""
    await purge_events_queue(rabbit)
    consumer_rabbit = Rabbit()
    await consumer_rabbit.connect()
    worker = Worker(seeded, consumer_rabbit, redis, index=index, render=fake_render, planner=planner)
    await worker.start()

    async def publish(source: dict, raw: dict, **kwargs: Any) -> dict:
        envelope = build_envelope(source_id=source["id"], raw=raw, priority=3, **kwargs)
        await rabbit.publish("events", "events.in", envelope, priority=3)
        return envelope

    try:
        yield SimpleNamespace(worker=worker, publish=publish, pool=seeded, rabbit=rabbit, redis=redis)
    finally:
        await consumer_rabbit.close()
        await worker.close()
        await purge_events_queue(rabbit)


async def alerts_for(pool, rule: FakeCompiledRule) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM alerts WHERE rule_id = $1 ORDER BY created_at", rule.rule.id)
    return [dict(r) for r in rows]
