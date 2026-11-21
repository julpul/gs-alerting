"""`/v1/preferences/me`: stored in the shape the routing layer reads."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.delivery.channel_select import in_window
from tests.escalation.helpers import user


@pytest.fixture(autouse=True)
async def no_prefs(seeded):
    await seeded.execute("DELETE FROM preferences WHERE user_id = $1", user("piotr@demo"))
    yield
    await seeded.execute("DELETE FROM preferences WHERE user_id = $1", user("piotr@demo"))


async def test_defaults_are_empty(client, auth_headers):
    resp = await client.get("/v1/preferences/me", headers=await auth_headers("piotr@demo"))
    assert resp.status_code == 200
    assert resp.json() == {"bySeverity": {}, "workingHours": None}


async def test_put_then_get_round_trips_and_matches_what_routing_reads(seeded, client, auth_headers):
    headers = await auth_headers("piotr@demo")
    body = {
        "bySeverity": {"critical": ["phone", "sms", "sms"], "warning": ["email"]},
        "workingHours": {"from": "08:00", "to": "18:00", "timezone": "Europe/Warsaw"},
    }
    saved = await client.put("/v1/preferences/me", json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["bySeverity"]["critical"] == ["phone", "sms"]  # duplicates collapse
    assert (await client.get("/v1/preferences/me", headers=headers)).json() == saved.json()

    row = await seeded.fetchrow(
        "SELECT by_severity, working_hours FROM preferences WHERE user_id = $1", user("piotr@demo")
    )
    assert row["by_severity"] == {"critical": ["phone", "sms"], "warning": ["email"]}
    assert row["working_hours"] == {"from": "08:00", "to": "18:00", "timezone": "Europe/Warsaw"}
    assert in_window(row["working_hours"], datetime(2026, 11, 20, 11, 0, tzinfo=UTC))  # 12:00 Warsaw
    assert not in_window(row["working_hours"], datetime(2026, 11, 20, 21, 0, tzinfo=UTC))

    cleared = await client.put("/v1/preferences/me", json={}, headers=headers)
    assert cleared.json() == {"bySeverity": {}, "workingHours": None}


async def test_preferences_are_per_user(client, auth_headers):
    await client.put(
        "/v1/preferences/me",
        json={"bySeverity": {"error": ["sms"]}},
        headers=await auth_headers("piotr@demo"),
    )
    other = await client.get("/v1/preferences/me", headers=await auth_headers("anna@demo"))
    assert other.json()["bySeverity"] != {"error": ["sms"]}


@pytest.mark.parametrize(
    ("body", "text"),
    [
        ({"bySeverity": {"urgent": ["sms"]}}, "urgent"),
        ({"bySeverity": {"critical": ["pigeon"]}}, "bySeverity"),
        ({"workingHours": {"from": "eight", "to": "18:00"}}, "HH:MM"),
        ({"workingHours": {"from": "08:00", "to": "18:00", "timezone": "Mars/Base"}}, "timezone"),
        ({"workingHours": {"to": "18:00"}}, "HH:MM"),
    ],
)
async def test_invalid_preferences_are_rejected(client, auth_headers, body, text):
    resp = await client.put("/v1/preferences/me", json=body, headers=await auth_headers("piotr@demo"))
    assert resp.status_code == 422
    assert text in resp.json()["message"]


async def test_requires_authentication(client):
    assert (await client.get("/v1/preferences/me")).status_code == 401
    assert (await client.put("/v1/preferences/me", json={})).status_code == 401
