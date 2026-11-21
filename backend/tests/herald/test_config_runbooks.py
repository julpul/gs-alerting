"""`/api/runbooks` (list, PUT create/update), `/api/runbooks/parse` for the three Appendix B formats."""

from __future__ import annotations

import sys
import types
from uuid import uuid4

import pytest

from app.core.ids import stable_id
from tests.herald.config_helpers import ANNA, MASTER, RUNBOOKS

RUNBOOK_KEYS = {
    "id",
    "title",
    "source_format",
    "source_text",
    "steps",
    "parsed_by",
    "reviewed",
    "link",
    "used_by",
}
STEP_KEYS = {"id", "kind", "text", "action", "params", "approval", "condition", "suggest"}


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr("app.runbooks.parsers.text.ai_available", lambda: False)


def _text(name: str) -> str:
    return (RUNBOOKS / name).read_text()


async def test_parse_appendix_b_formats(client):
    yaml_rb = (
        await client.post(
            "/api/runbooks/parse", json={"text": _text("settlement-timeout.yaml")}, headers=MASTER
        )
    ).json()
    assert (
        set(yaml_rb) >= RUNBOOK_KEYS
        and yaml_rb["source_format"] == "yaml"
        and yaml_rb["parsed_by"] == "parser"
    )
    assert [(s["id"], s["kind"], s["action"]) for s in yaml_rb["steps"]] == [
        (1, "check", "http_get"),
        (2, "action", "retry_workflow"),
        (3, "action", "escalate"),
    ]
    assert yaml_rb["steps"][1]["params"] == {"workflowId": "{{workflowId}}"}
    assert yaml_rb["steps"][2]["approval"] == "required" and yaml_rb["steps"][2]["condition"]
    assert all(set(s) >= STEP_KEYS for s in yaml_rb["steps"])

    md = (
        await client.post("/api/runbooks/parse", json={"text": _text("HighErrorRate.md")}, headers=MASTER)
    ).json()
    assert md["source_format"] == "markdown" and len(md["steps"]) == 4
    assert md["steps"][1]["suggest"] == "failover_region" and md["steps"][1]["action"] is None
    assert md["steps"][0]["kind"] == "manual"

    txt = (
        await client.post("/api/runbooks/parse", json={"text": _text("pricing-latency.txt")}, headers=MASTER)
    ).json()
    assert txt["source_format"] == "text" and len(txt["steps"]) >= 3
    assert "restart_service" in {s["suggest"] for s in txt["steps"]}

    link = (
        await client.post(
            "/api/runbooks/parse", json={"text": "https://wiki.example/runbook"}, headers=MASTER
        )
    ).json()
    assert link["source_format"] == "link" and link["link"] == "https://wiki.example/runbook"
    empty = await client.post("/api/runbooks/parse", json={"text": "  "}, headers=MASTER)
    assert empty.status_code == 422 and "empty" in empty.json()["detail"]


async def test_parse_uses_the_ai_parser_when_available(client, monkeypatch):
    module = types.ModuleType("app.ai.runbook_parse")

    async def parse_text(text):
        return [
            {"text": "Restart pricing-engine-3", "action": "restart_service", "parameters": {"service": "x"}}
        ]

    module.parse_text = parse_text
    monkeypatch.setitem(sys.modules, "app.ai.runbook_parse", module)
    monkeypatch.setattr("app.runbooks.parsers.text.ai_available", lambda: True)
    rb = (
        await client.post(
            "/api/runbooks/parse",
            json={"text": _text("pricing-latency.txt"), "format": "text"},
            headers=MASTER,
        )
    ).json()
    assert rb["parsed_by"] == "llm" and rb["steps"][0]["suggest"] == "restart_service"


async def test_list_runbooks_shape_and_used_by(client):
    rows = (await client.get("/api/runbooks", headers=MASTER)).json()
    by_id = {r["id"]: r for r in rows}
    settle = by_id[str(stable_id("runbook", "settlement-timeout"))]
    assert set(settle) >= RUNBOOK_KEYS
    assert settle["source_format"] == "yaml" and settle["reviewed"] is True
    assert any(name.startswith("Settlements") for name in settle["used_by"])
    assert settle["steps"][0]["id"] == 1 and settle["steps"][0]["key"] == "s1"
    md = by_id[str(stable_id("runbook", "HighErrorRate"))]
    assert md["matches"] == {"alertname": "HighErrorRate"} and md["reviewed"] is False


async def test_put_creates_then_updates(client, seeded):
    slug = f"ledger-lag-{uuid4().hex[:6]}"
    headers = {**ANNA}
    created = await client.put(
        f"/api/runbooks/{slug}",
        json={"text": "1. Check the ledger queue.\n2. Restart the consumer.", "title": "Ledger lag"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    rb = created.json()
    assert set(rb) >= RUNBOOK_KEYS and rb["title"] == "Ledger lag" and len(rb["steps"]) == 2
    again = await client.put(
        f"/api/runbooks/{slug}",
        json={"text": "- [ ] Check the queue\n- [ ] Page the on-call", "format": "markdown"},
        headers=headers,
    )
    assert again.json()["id"] == rb["id"] and again.json()["title"] == "Ledger lag"
    by_uuid = await client.put(
        f"/api/runbooks/{rb['id']}", json={"text": "Just wait.", "title": "Lag"}, headers=headers
    )
    assert (
        by_uuid.status_code == 200
        and by_uuid.json()["title"] == "Lag"
        and by_uuid.json()["source_format"] == "text"
    )
    assert any(r["id"] == rb["id"] for r in (await client.get("/api/runbooks", headers=headers)).json())
    audit = await seeded.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'runbook.update' AND target_id = $1", rb["id"]
    )
    assert audit == 2


async def test_put_rejections(client):
    assert (await client.put("/api/runbooks/x-1", json={"text": " "}, headers=MASTER)).status_code == 422
    missing = await client.put(f"/api/runbooks/{uuid4()}", json={"text": "1. a"}, headers=MASTER)
    assert missing.status_code == 404
    seeded_rates = str(stable_id("runbook", "settlement-timeout"))
    denied = await client.put(f"/api/runbooks/{seeded_rates}", json={"text": "1. a"}, headers=ANNA)
    assert denied.status_code == 404
