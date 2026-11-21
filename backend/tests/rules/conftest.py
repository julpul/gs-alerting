from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from app.core.ids import stable_id, uuid7
from app.pipeline.fields import EventView

InsertRule = Callable[..., Awaitable[UUID]]


def cond(field: str, operator: str, value=None) -> dict:
    return {"field": field, "operator": operator, "value": value}


def definition(
    *conditions: dict, name: str = "t", op: str = "and", team: str = "team-rates", **extra
) -> dict:
    return {
        "name": name,
        "teamId": str(stable_id("team", team)),
        "match": {"op": op, "conditions": list(conditions)},
        **extra,
    }


@pytest_asyncio.fixture
async def insert_rule(seeded) -> InsertRule:
    """Insert a rule row straight into the DB (no API); returns its id. Rows are removed after the test."""
    created: list[UUID] = []

    async def _insert(defn: dict, *, enabled: bool = True) -> UUID:
        rule_id = uuid7()
        await seeded.execute(
            "INSERT INTO rules (id, team_id, name, enabled, version, definition) "
            "VALUES ($1, $2, $3, $4, 1, $5)",
            rule_id,
            UUID(defn["teamId"]),
            defn["name"],
            enabled,
            defn,
        )
        created.append(rule_id)
        return rule_id

    yield _insert
    await seeded.execute("DELETE FROM rules WHERE id = ANY($1)", created)


@pytest.fixture
def unique() -> str:
    return uuid4().hex[:10]


def view(raw: dict, **meta) -> EventView:
    return EventView(raw, meta)
