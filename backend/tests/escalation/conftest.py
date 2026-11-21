"""Fixtures for the escalation tests."""

from __future__ import annotations

import pytest

from app.config import get_settings
from tests.escalation.helpers import FakeRabbit, settle


@pytest.fixture(autouse=True)
async def quiet_outbox(seeded):
    """Rows written by planning tests must never be picked up by workers or relays of other tests."""
    await settle(seeded)
    yield
    await settle(seeded)


@pytest.fixture
def fake_rabbit() -> FakeRabbit:
    return FakeRabbit()


@pytest.fixture
def fast_mode(monkeypatch):
    """`DEMO_FAST_ESCALATION=true`: policy minutes become seconds."""
    monkeypatch.setenv("DEMO_FAST_ESCALATION", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()
