"""Fixtures for the runbook tests: seeded org + runbooks, in-process mockgw, deterministic parsing."""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.delivery.conftest import gw, mockgw_server  # noqa: F401  (re-exported fixtures)


@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def approvals_short_id(pool):
    """P3-ESCAL adds `approvals.short_id` (bigserial) in migration 05x; until it is merged, add it here."""
    await pool.execute("ALTER TABLE approvals ADD COLUMN IF NOT EXISTS short_id bigserial")


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_trust(seeded):
    await seeded.execute("DELETE FROM action_trust")
    yield


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Free-text parsing must never reach a real LLM in tests."""
    monkeypatch.setattr("app.runbooks.parsers.text.ai_available", lambda: False)
