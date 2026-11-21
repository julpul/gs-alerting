"""Draft tools: AI proposals and dry-runs; nothing is ever saved."""

from __future__ import annotations

from uuid import uuid4

from app.core.ids import stable_id

from .helpers import call, call_error

RULE = {
    "name": "Payments critical to on-call",
    "teamId": "team-payments",
    "match": {"op": "and", "conditions": [{"field": "_severity", "operator": "eq", "value": "critical"}]},
    "routing": {"hard": [{"kind": "oncall", "id": "team-payments"}], "soft": []},
    "dedup": {"fields": ["_service"], "windowSeconds": 300},
}
SAMPLE = {"labels": {"severity": "CRITICAL", "alertname": "HighErrorRate"}, "summary": "5xx above 5%"}


async def test_propose_rule_returns_a_draft_with_a_dry_run_and_saves_nothing(make_pat, connect, seeded, llm):
    before = await seeded.fetchval("SELECT count(*) FROM rules")
    llm.responses = {"smart": {"rule": RULE, "explanation": "Pages payments on-call for critical events."}}
    pat = await make_pat("anna@demo", f"drafter-{uuid4().hex[:6]}")
    async with connect(pat.token) as agent:
        out = await call(
            agent, "propose_rule", {"text": "critical payments events page on-call", "team": "team-payments"}
        )
        assert out["status"] == "draft" and out["draft"]["name"] == RULE["name"]
        assert out["explanation"].startswith("Pages payments") and "evaluated" in out["dryRun"]
        assert llm.calls[0]["tier"] == "smart"

        assert "forbidden" in await call_error(
            agent, "propose_rule", {"text": "anything", "team": "team-settlements"}
        )
        llm.fail = True
        assert "ai_unavailable" in await call_error(
            agent, "propose_rule", {"text": "critical payments events", "team": "team-payments"}
        )
    assert await seeded.fetchval("SELECT count(*) FROM rules") == before


async def test_test_rule_dry_runs_and_validates(make_pat, connect):
    pat = await make_pat("anna@demo", f"tester-{uuid4().hex[:6]}")
    async with connect(pat.token) as agent:
        result = await call(agent, "test_rule", {"rule": RULE})
        assert {"evaluated", "matched", "alertsAfterDedup", "notifications"} <= set(result)
        broken = {**RULE, "match": {"op": "and", "conditions": [{"field": "x", "operator": "nope"}]}}
        assert "invalid_rule" in await call_error(agent, "test_rule", {"rule": broken})
        assert "invalid_rule" in await call_error(agent, "test_rule", {"rule": {**RULE, "teamId": "nowhere"}})
        assert "forbidden" in await call_error(
            agent, "test_rule", {"rule": {**RULE, "teamId": "team-settlements"}}
        )


async def test_propose_schema_mapping_uses_the_model_then_the_heuristic(make_pat, connect, seeded, llm):
    source_id = uuid4()
    await seeded.execute(
        "INSERT INTO sources (id, name, team_id, domain_id, type, key_prefix, key_hash) "
        "VALUES ($1, $2, $3, $4, 'webhook', 'sk_x', $5)",
        source_id,
        f"mapping-src-{source_id.hex[:6]}",
        stable_id("team", "team-payments"),
        stable_id("domain", "payments"),
        f"hash-{source_id}",
    )
    llm.responses = {"fast": {"fields": {"severity": "labels.severity"}, "explanation": "AI says so"}}
    anna = await make_pat("anna@demo", f"mapper-{uuid4().hex[:6]}")
    piotr = await make_pat("piotr@demo", f"mapper-{uuid4().hex[:6]}")
    async with connect(anna.token) as agent:
        out = await call(agent, "propose_schema_mapping", {"source_id": str(source_id), "sample": SAMPLE})
        assert out["status"] == "draft" and out["fields"]["severity"] == "labels.severity"
        llm.fail = True
        fallback = await call(
            agent, "propose_schema_mapping", {"source_id": str(source_id), "sample": SAMPLE}
        )
        assert fallback["status"] == "draft" and "severity" in fallback["fields"]
        assert "not_found" in await call_error(
            agent, "propose_schema_mapping", {"source_id": "nope", "sample": SAMPLE}
        )
    async with connect(piotr.token) as agent:
        assert "not_found" in await call_error(
            agent, "propose_schema_mapping", {"source_id": str(source_id), "sample": SAMPLE}
        )
    await seeded.execute("DELETE FROM sources WHERE id = $1", source_id)


async def test_propose_runbook_parses_text_into_draft_steps(make_pat, connect):
    pat = await make_pat("anna@demo", f"rb-{uuid4().hex[:6]}")
    text = "1. Check the dashboard\n2. Restart the service (needs approval)\n3. Verify the health endpoint"
    async with connect(pat.token) as agent:
        out = await call(agent, "propose_runbook", {"text": text})
        assert out["status"] == "draft" and len(out["steps"]) == 3
        assert any(s.get("approval") == "required" for s in out["steps"])
        assert "validation_error" in await call_error(agent, "propose_runbook", {"text": "   "})
