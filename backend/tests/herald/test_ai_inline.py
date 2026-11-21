"""AI inside the classic screens: the drawer's suggestion state, rule drafts in the Herald shape, runbook
"Write with AI" (parse + save exactly the reviewed steps). The LLM is the stub from `tests/fakes/llm.py`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.ai.llm import set_llm
from app.api.routers.herald_suggestions import model_name
from app.core.ids import uuid7
from tests.fakes.llm import FakeLLM
from tests.herald.config_helpers import ANNA

from .core_helpers import alert, as_person

PAYMENTS_RULE = {
    "name": "Payments critical to on-call",
    "teamId": "team-payments",
    "match": {"op": "and", "conditions": [{"field": "_severity", "operator": "eq", "value": "critical"}]},
    "routing": {"hard": [{"kind": "oncall", "id": "team-payments"}], "soft": []},
    "dedup": {"fields": ["_service"], "windowSeconds": 300},
}
AI_STEPS = {
    "steps": [
        {"text": "Check the queue depth of the ledger consumer", "kind": "check"},
        {
            "text": "Restart the ledger consumer",
            "kind": "action",
            "action": "restart_service",
            "parameters": {"service": "ledger-consumer"},
            "risk": "high",
        },
    ]
}


@pytest.fixture
def llm():
    fake = FakeLLM()
    set_llm(fake)
    yield fake
    set_llm(None)


async def _suggest(client, short_id: int, person: str = "anna"):
    return await client.get(f"/api/alerts/{short_id}/suggestion", headers=as_person(person))


async def test_suggestion_states_pending_ready_none_and_scope(client, seeded):
    row = await alert(seeded, title="AI inline pending", severity="critical", flags=["suspicious_content"])
    r = await _suggest(client, row["short_id"])
    assert r.status_code == 200, r.text
    assert r.json() == {"state": "pending", "suggestion": None, "model": None, "suspicious": True}

    await seeded.execute(
        "INSERT INTO suggestions (id, alert_id, status, content, model, latency_ms) "
        "VALUES ($1, $2, 'ready', $3, 'anthropic/claude-haiku-5-5', 900)",
        uuid7(),
        row["id"],
        {"diagnosis": "The ledger consumer is stuck.", "nextStep": "Restart it", "confidence": 0.7},
    )
    body = (await _suggest(client, row["short_id"])).json()
    assert body["state"] == "ready" and body["model"] == "claude-haiku-5-5"
    sg = body["suggestion"]
    assert sg["diagnosis"] == "The ledger consumer is stuck." and sg["source"] == "ai"
    assert sg["status"] == "draft" and sg["alertId"] == str(row["id"])

    # another team: same answer as a missing alert
    assert (await _suggest(client, row["short_id"], "piotr")).status_code == 404
    assert (await client.get("/api/alerts/nope/suggestion", headers=ANNA)).status_code == 404


async def test_suggestion_degraded_has_no_model(client, seeded):
    row = await alert(seeded, title="AI inline degraded", severity="error")
    await seeded.execute(
        "INSERT INTO suggestions (id, alert_id, status, content, model) "
        "VALUES ($1, $2, 'degraded', $3, NULL)",
        uuid7(),
        row["id"],
        {"aiUnavailable": True, "fixedBefore": []},
    )
    body = (await _suggest(client, row["short_id"])).json()
    assert body["state"] == "ready" and body["model"] is None
    assert body["suggestion"]["source"] == "heuristic" and body["suggestion"]["aiUnavailable"] is True


async def test_no_suggestion_is_coming_for_resolved_old_or_quiet_alerts(client, seeded):
    resolved = await alert(seeded, title="AI inline resolved", status="resolved", severity="critical")
    quiet = await alert(seeded, title="AI inline info", severity="info")
    old = await alert(seeded, title="AI inline old", severity="critical")
    await seeded.execute(
        "UPDATE alerts SET created_at = $2 WHERE id = $1", old["id"], datetime.now(UTC) - timedelta(hours=1)
    )
    for row in (resolved, quiet, old):
        assert (await _suggest(client, row["short_id"])).json()["state"] == "none"


def test_model_name():
    assert model_name("anthropic/claude-haiku-5-5") == "claude-haiku-5-5"
    assert model_name("smart:openai/gpt-x (reused)") == "gpt-x"
    assert model_name(None) is None and model_name("") is None


async def test_rule_draft_from_text_in_the_herald_shape(client, seeded, llm):
    llm.responses = {"smart": {"rule": PAYMENTS_RULE, "explanation": "Pages the on-call person."}}
    r = await client.post(
        "/api/rules/draft-from-text",
        json={"text": "call payments on-call for anything critical", "owner": "team:team-payments"},
        headers=ANNA,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "ai" and body["explanation"] == "Pages the on-call person."
    doc = body["doc"]
    assert doc["id"] is None and doc["owner"] == "team:team-payments"
    assert doc["name"] == "Payments critical to on-call"
    assert doc["when"] == {"all": [{"field": "@severity", "op": "eq", "value": "critical"}]}
    assert doc["notify"][0]["to"] == "team:team-payments/on-call"
    assert {"evaluated", "matched", "notifications"} <= set(body["dryRun"])
    assert "<untrusted_event>" in llm.calls[0]["user"]

    # the team the UI acts for is used when no owner is given
    r = await client.post(
        "/api/rules/draft-from-text",
        json={"text": "same again"},
        headers={**ANNA, "X-Herald-Teams": "team-payments"},
    )
    assert r.status_code == 200 and r.json()["doc"]["owner"] == "team:team-payments"


async def test_rule_draft_failures(client, seeded, llm):
    llm.fail = True
    body = {"text": "page someone", "owner": "team:team-payments"}
    r = await client.post("/api/rules/draft-from-text", json=body, headers=ANNA)
    assert r.status_code == 503 and "not available" in r.json()["detail"]
    r = await client.post(
        "/api/rules/draft-from-text",
        json={"text": "page someone", "owner": "team:team-settlements"},
        headers=ANNA,
    )
    assert r.status_code == 403
    blank = await client.post(
        "/api/rules/draft-from-text", json={"text": "   ", "owner": "team:team-payments"}, headers=ANNA
    )
    assert blank.status_code == 422
    assert llm.calls == [] or all("page someone" in c["user"] for c in llm.calls)


async def test_runbook_write_with_ai_then_save_the_reviewed_steps(client, seeded, llm, monkeypatch):
    monkeypatch.setattr("app.runbooks.parsers.text.ai_available", lambda: True)
    llm.responses = {"smart": AI_STEPS}
    text = "1. Check the ledger queue. 2. If it is stuck restart the consumer, carefully."
    r = await client.post("/api/runbooks/parse", json={"text": text, "ai": True}, headers=ANNA)
    assert r.status_code == 200, r.text
    rb = r.json()
    assert rb["parsed_by"] == "llm" and rb["source"] == "ai"
    assert [s["action"] for s in rb["steps"]] == [None, "restart_service"]
    assert rb["steps"][1]["approval"] == "required"
    draft = rb["draft_steps"]
    assert len(draft) == 2 and draft[1]["parameters"] == {"service": "ledger-consumer"}

    calls = len(llm.calls)
    slug = f"ledger-ai-{uuid4().hex[:6]}"
    saved = await client.put(
        f"/api/runbooks/{slug}",
        json={"text": text, "title": "Ledger (AI)", "steps": draft, "ai": True},
        headers=ANNA,
    )
    assert saved.status_code == 200, saved.text
    out = saved.json()
    assert out["parsed_by"] == "llm" and out["reviewed"] is False and out["title"] == "Ledger (AI)"
    assert [s["text"] for s in out["steps"]] == [s["text"] for s in rb["steps"]]
    assert len(llm.calls) == calls  # saved what was reviewed: no second model call


async def test_runbook_split_with_fixed_rules_never_asks_the_model(client, seeded, llm, monkeypatch):
    monkeypatch.setattr("app.runbooks.parsers.text.ai_available", lambda: True)
    r = await client.post(
        "/api/runbooks/parse", json={"text": "Check the queue. Then restart it.", "ai": False}, headers=ANNA
    )
    assert r.status_code == 200 and r.json()["parsed_by"] == "parser" and llm.calls == []
    assert "draft_steps" not in r.json()


async def test_runbook_write_with_ai_falls_back_when_the_model_is_down(client, seeded, llm, monkeypatch):
    monkeypatch.setattr("app.runbooks.parsers.text.ai_available", lambda: True)
    llm.fail = True
    r = await client.post(
        "/api/runbooks/parse", json={"text": "Check the queue. Then restart it.", "ai": True}, headers=ANNA
    )
    assert r.status_code == 200 and r.json()["source"] == "heuristic" and r.json()["steps"]
