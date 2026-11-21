from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import Recorder, make_sim
from vistula_sim.app import build_simulator, create_app
from vistula_sim.config import Settings


@pytest.fixture
def client_and_rec():
    rec = Recorder()
    sim = make_sim(rec.handler, baseline_enabled=False)
    app = create_app(sim.settings, sim)
    with TestClient(app) as client:
        yield client, rec, sim


def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def test_healthz(client_and_rec):
    client, _, _ = client_and_rec
    assert client.get("/healthz").json() == {"status": "ok"}


def test_panel_is_html_and_polls_stats(client_and_rec):
    client, _, _ = client_and_rec
    response = client.get("/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    assert "Vistula Capital simulator" in response.text and 'fetch("stats")' in response.text
    assert "setInterval(refresh, 1000)" in response.text


def test_scenarios_listing(client_and_rec):
    client, _, _ = client_and_rec
    names = {s["name"] for s in client.get("/scenarios").json()}
    assert {"storm_500", "poisoned_log", "new_schema", "resolve_all"} <= names


def test_stats_shape(client_and_rec):
    client, _, _ = client_and_rec
    stats = client.get("/stats").json()
    assert {"baseline", "sources", "totals", "scenarios", "last", "targetApiUrl"} <= set(stats)
    assert stats["sources"]["prometheus"] == {"sent": 0, "errors": 0}


def test_post_scenario_runs_and_counts(client_and_rec):
    client, rec, _ = client_and_rec
    response = client.post("/scenario/poisoned_log")
    assert response.status_code == 202 and response.json()["steps"] == 1
    wait_for(lambda: len(rec.requests) == 1)
    assert "ignore previous instructions" in rec.bodies()[0]["message"]
    wait_for(lambda: client.get("/stats").json()["sources"]["pricing-logs"]["sent"] == 1)


def test_post_scenario_with_intensity_and_params(client_and_rec):
    client, rec, _ = client_and_rec
    response = client.post("/scenario/new_schema?intensity=2&sourceKey=custom")
    assert response.json()["steps"] == 6
    wait_for(lambda: len(rec.requests) == 6)
    assert {r.url.path for r in rec.requests} == {"/v1/ingest/custom"}


def test_unknown_scenario_is_404(client_and_rec):
    client, _, _ = client_and_rec
    response = client.post("/scenario/nope")
    assert response.status_code == 404 and "storm_500" in response.json()["detail"]


def test_baseline_update(client_and_rec):
    client, _, sim = client_and_rec
    body = client.post("/baseline", json={"rate": 7.5, "paused": True, "autoIncidents": False}).json()
    assert body["baseRate"] == 7.5 and body["paused"] is True and body["autoIncidents"] is False
    assert sim.base_rate == 7.5 and sim.current_rate() == 0.0


def test_lifespan_starts_baseline_when_enabled():
    rec = Recorder()
    sim = make_sim(rec.handler, baseline_enabled=True, base_rate=2000.0)
    with TestClient(create_app(sim.settings, sim)):
        wait_for(lambda: len(rec.requests) > 0)


def test_build_simulator_wires_amqp_only_when_configured():
    transport = httpx.MockTransport(lambda _: httpx.Response(202))
    assert build_simulator(Settings(), transport).amqp is None
    sim = build_simulator(Settings(customer_amqp_url="amqp://guest:guest@localhost/"), transport)
    assert sim.amqp is not None


def test_create_app_builds_defaults_from_env(monkeypatch):
    monkeypatch.setenv("BASELINE_ENABLED", "false")
    app = create_app()
    assert app.state.simulator.settings.baseline_enabled is False
