"""Fixtures for the AI tests: the stub LLM is installed for every test (and removed afterwards)."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest

from app.ai.llm import set_llm
from app.core.ids import uuid7
from tests.fakes.llm import FakeLLM


@pytest.fixture(autouse=True)
def llm():
    fake = FakeLLM()
    set_llm(fake)
    yield fake
    set_llm(None)


@pytest.fixture
def runbook_for(monkeypatch) -> Callable[[list[dict[str, Any]] | None], None]:
    """Install a fake `app.runbooks.lookup` returning the given steps (None = no runbook)."""

    class View:
        def __init__(self, steps):
            self.steps = steps

        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            return {"id": "rb-1", "name": "Settlement runbook", "steps": self.steps}

    def install(steps: list[dict[str, Any]] | None) -> None:
        async def runbook_for_alert(conn, alert):
            return View(steps) if steps is not None else None

        module = types.ModuleType("app.runbooks.lookup")
        module.runbook_for_alert = runbook_for_alert
        monkeypatch.setitem(sys.modules, "app.runbooks.lookup", module)

    return install


async def add_resolution(pool, alert, note: str, *, team_id: UUID | None = None) -> UUID:
    resolution_id = uuid7()
    await pool.execute(
        "INSERT INTO resolutions (id, alert_id, domain_id, team_id, knowledge_key, note) "
        "SELECT $1, a.id, a.domain_id, COALESCE($3, a.team_id), a.knowledge_key, $2 FROM alerts a "
        "WHERE a.id = $4",
        resolution_id,
        note,
        team_id,
        alert["id"],
    )
    return resolution_id
