"""mockgw unit tests. Run from the repo root: `cd backend && uv run pytest ../mockgw/tests`."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


@pytest.fixture(autouse=True)
def clean_state():
    main.clock = lambda: 1000.0
    TestClient(main.app).post("/admin/reset")
    yield
    main.clock = __import__("time").monotonic


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def test_sms_recorded_and_idempotent(client):
    body = {"to": "+48500100200", "text": "[CRIT] A-1 boom"}
    first = client.post("/sms", json=body, headers={"Idempotency-Key": "k1"})
    second = client.post("/sms", json=body, headers={"Idempotency-Key": "k1"})
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["deduplicated"] is True
    assert len(client.get("/admin/state").json()["sms"]) == 1
    client.post("/sms", json=body, headers={"Idempotency-Key": "k2"})
    assert len(client.get("/admin/state").json()["sms"]) == 2


def test_call_recorded_and_idempotent(client):
    body = {"to": "+48500100201", "text": "Alert A-7. Press 1 to acknowledge."}
    client.post("/call", json=body, headers={"Idempotency-Key": "c1"})
    client.post("/call", json=body, headers={"Idempotency-Key": "c1"})
    calls = client.get("/admin/state").json()["calls"]
    assert len(calls) == 1 and calls[0]["status"] == "ringing"


def test_fail_toggle_returns_503_then_expires(client):
    assert client.post("/admin/fail", json={"channel": "sms", "seconds": 30}).json()["failing"] is True
    resp = client.post("/sms", json={"to": "x", "text": "y"})
    assert resp.status_code == 503
    assert client.post("/call", json={"to": "x", "text": "y"}).status_code == 200  # other channel unaffected
    state = client.get("/admin/state").json()
    assert state["sms"] == [] and state["failures"]["sms"]["remainingSeconds"] == 30
    main.clock = lambda: 1031.0  # past the outage
    assert client.post("/sms", json={"to": "x", "text": "y"}).status_code == 200


def test_fail_clear_and_counted_failures(client):
    client.post("/admin/fail", json={"channel": "sms", "seconds": 60})
    client.post("/admin/fail", json={"channel": "sms", "seconds": 0})
    assert client.post("/sms", json={"to": "x"}).status_code == 200
    client.post("/admin/fail", json={"channel": "sms", "count": 2})
    codes = [client.post("/sms", json={"to": "x"}).status_code for _ in range(3)]
    assert codes == [503, 503, 200]


def test_fail_all_channels(client):
    client.post("/admin/fail", json={"channel": "all", "seconds": 10})
    assert client.post("/sms", json={"to": "x"}).status_code == 503
    assert client.post("/call", json={"to": "x"}).status_code == 503


def test_reset_clears_state_and_failures(client):
    client.post("/sms", json={"to": "x"})
    client.post("/admin/fail", json={"channel": "phone", "seconds": 60})
    assert client.post("/admin/reset").json() == {"status": "reset"}
    state = client.get("/admin/state").json()
    assert state["sms"] == [] and state["failures"] == {}
    assert client.post("/call", json={"to": "x"}).status_code == 200


def test_workflow_retry_success_and_configured_failure(client):
    ok = client.post("/workflows/wf-1/retry")
    assert ok.status_code == 200 and ok.json() == {"workflowId": "wf-1", "status": "retried", "attempt": 1}
    assert client.post("/workflows/wf-1/retry").json()["attempt"] == 2
    client.post("/admin/fail", json={"channel": "workflow", "count": 2})
    assert client.post("/workflows/wf-1/retry").status_code == 500
    assert client.post("/workflows/wf-1/retry").status_code == 500
    assert client.post("/workflows/wf-1/retry").status_code == 200
    results = [w["result"] for w in client.get("/admin/state").json()["workflows"]]
    assert results == ["retried", "retried", "failed", "failed", "retried"]


def test_service_restart_and_health(client):
    assert client.get("/svc/counterparty/health").json() == {"service": "counterparty", "status": "ok"}
    client.post("/admin/fail", json={"channel": "svc:counterparty", "seconds": 60})
    assert client.get("/svc/counterparty/health").status_code == 503
    restart = client.post("/services/counterparty/restart")
    assert restart.json() == {"service": "counterparty", "status": "restarted"}
    assert client.get("/svc/counterparty/health").status_code == 200
    assert client.get("/admin/state").json()["restarts"][0]["service"] == "counterparty"


def test_tickets_failover_rollback(client):
    t1 = client.post("/tickets", json={"summary": "Settlement stuck"}).json()
    t2 = client.post("/tickets", json={"summary": "Another"}).json()
    assert (t1["id"], t2["id"]) == ("TCK-1", "TCK-2") and t1["summary"] == "Settlement stuck"
    assert [t["id"] for t in client.get("/tickets").json()] == ["TCK-1", "TCK-2"]
    assert client.post("/failover", json={"region": "eu-central-1"}).json()["status"] == "failed_over"
    assert client.post("/rollback").json()["status"] == "rolled_back"
    state = client.get("/admin/state").json()
    assert state["failovers"][0]["region"] == "eu-central-1" and len(state["rollbacks"]) == 1
    client.post("/admin/fail", json={"channel": "tickets", "seconds": 5})
    assert client.post("/tickets", json={}).status_code == 503


def test_index_page_lists_sms_and_calls_with_reply_and_press_one(client):
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Press 1 to ACK" in page.text and "setInterval(load, 2000)" in page.text
    assert "textContent" in page.text and "innerHTML" not in page.text  # alert text is untrusted


def _backend(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        main,
        "_backend_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://backend"),
    )
    return seen


def test_reply_is_forwarded_to_backend_inbound(client, monkeypatch):
    seen = _backend(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))
    resp = client.post("/reply", json={"from": "+48500100200", "text": "ACK 1234"})
    assert resp.status_code == 200 and resp.json()["backendStatus"] == 200
    assert seen[0].url.path == "/v1/inbound/sms"
    assert seen[0].content == b'{"from":"+48500100200","text":"ACK 1234"}'
    assert client.get("/admin/state").json()["replies"][0]["text"] == "ACK 1234"


def test_reply_forwards_inbound_secret_header_when_configured(client, monkeypatch):
    monkeypatch.setenv("INBOUND_SMS_SECRET", "s3cret")
    seen = _backend(monkeypatch, lambda r: httpx.Response(200, json={}))
    client.post("/reply", json={"from": "+48500100200", "text": "ACK 1"})
    assert seen[0].headers["x-inbound-secret"] == "s3cret"


def test_reply_sends_no_secret_header_by_default(client, monkeypatch):
    monkeypatch.delenv("INBOUND_SMS_SECRET", raising=False)
    seen = _backend(monkeypatch, lambda r: httpx.Response(200, json={}))
    client.post("/reply", json={"from": "+48500100200", "text": "ACK 1"})
    assert "x-inbound-secret" not in seen[0].headers


def test_reply_backend_down_is_502(client, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")

    _backend(monkeypatch, boom)
    assert client.post("/reply", json={"from": "x", "text": "ACK 1"}).status_code == 502


def test_press_one_acks_alert_named_in_call(client, monkeypatch):
    seen = _backend(monkeypatch, lambda r: httpx.Response(200, json={}))
    call = client.post("/call", json={"to": "+48500100201", "text": "Alert A-42, severity critical."}).json()
    resp = client.post(f"/calls/{call['id']}/press", json={"digit": "1"})
    assert resp.status_code == 200
    assert b"ACK 42" in seen[0].content and b"+48500100201" in seen[0].content
    assert client.get("/admin/state").json()["calls"][0]["status"] == "answered"
    other = client.post(f"/calls/{call['id']}/press", json={"digit": "2"})
    assert other.json()["pressed"] == "2" and len(seen) == 1
    assert client.post("/calls/nope/press", json={"digit": "1"}).status_code == 404


def test_press_one_without_alert_reference_is_422(client, monkeypatch):
    _backend(monkeypatch, lambda r: httpx.Response(200))
    call = client.post("/call", json={"to": "x", "text": "hello"}).json()
    assert client.post(f"/calls/{call['id']}/press", json={"digit": "1"}).status_code == 422


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
